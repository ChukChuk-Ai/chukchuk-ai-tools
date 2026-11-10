"""문서 부품: 마크다운 → PDF·Word(docx)·PowerPoint(pptx)·md·txt 만들기, PDF 글 뽑기.

    doclib.export(마크다운, 제목, 폴더, ("pdf",))   → {"saved": [경로], "failed": [이유]}
    doclib.read_pdf(경로, 시작쪽, 끝쪽)          → 쪽별 글

패키지(reportlab, python-docx, python-pptx, pypdf)는 도구 자기 환경(.venv)에 있다(tool.json 의 requirements).
글꼴은 sdk.system.document_font(OS별), 보고서에 찍는 앱 이름은 sdk.config.app_name()(화면 언어).
"""
from __future__ import annotations

import html
import io
import math
import os
import re
import time
import unicodedata
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from sdk import config, system, text

FONT, FONT_FILE, FONT_BOLD = system.document_font()   # 한글이 되는 글꼴(OS별)
REPORT = {"ko": "보고서", "en": "Report"}
T = text.load(Path(__file__).parent)   # 사용자에게 보일 안내·오류 글(texts.toml)


def brand() -> str:
    """문서에 찍는 이름(예: '척척아이 보고서', 'ChukChuk AI Report')."""
    return f"{config.app_name()} {REPORT.get(config.language(), REPORT['en'])}"
BLUE = "244766"
PALE = "F0F5FA"


def plain(text):
    text = re.sub(r"!\[([^]]*)\]\(([^)]+)\)", lambda m: T("doc_image", alt=m.group(1)), text)
    text = re.sub(r"\[([^]]+)\]\(<?([^)>]+)>?\)", r"\1 (\2)", text)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</?(?:b|strong|em|i|code|p|span)[^>]*>", "", text, flags=re.I)
    value = html.unescape(re.sub(r"\*\*(.*?)\*\*|`([^`]+)`", lambda m: m[1] if m[1] is not None else m[2], text)).replace("\\|", "|")
    return re.sub(r"\\([\\`*\[\]_])", r"\1", value)


def _cells(line):
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))]


def blocks(text):
    """제목·목록·GFM 표를 공통 구조로 바꾼다. 원문 글/숫자/출처는 그대로 유지한다."""
    lines = text.splitlines()
    result, i = [], 0
    while i < len(lines):
        line = lines[i].strip()
        if not line or re.fullmatch(r"[-*_]{3,}", line):
            i += 1
            continue
        if i+1 < len(lines) and "|" in line and all(re.fullmatch(r":?-{3,}:?", c) for c in _cells(lines[i+1])):
            header = _cells(line)
            rows, i = [], i+2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                row = _cells(lines[i])
                rows.append((row+[""]*len(header))[:len(header)])
                i += 1
            result.append(("table", header, rows))
            continue
        heading = re.match(r"^(#{1,6})\s+(.+)", line)
        if heading:
            result.append(("heading", min(3,len(heading[1])), heading[2]))
        elif re.match(r"^>\s*\[![\w]+\]$", line):
            pass  # 뒤따르는 실제 주의 문장은 일반 문단으로 보존한다.
        elif line.startswith(">"):
            result.append(("note", line.lstrip("> ")))
        elif re.match(r"^[-*+]\s+", line):
            result.append(("bullet", re.sub(r"^[-*+]\s+", "", line)))
        elif re.match(r"^\d+[.)]\s+",line):
            result.append(("paragraph",line))
        elif line.startswith("```"):
            pass
        else:
            result.append(("paragraph",line))
        i += 1
    return result


def column_weights(header, rows):
    data=[header,*rows]
    weights=[math.sqrt(max(8,min(100,max(len(plain(str(row[i]))) for row in data)))) for i in range(len(header))]
    total=sum(weights)
    return [value/total for value in weights]


def wrap_text(text, units):
    lines=[]
    for original in plain(text).split("\n"):
        line, width="",0
        for char in original:
            size=2 if unicodedata.east_asian_width(char) in "WF" else 1
            if line and width+size>units:
                lines.append(line)
                line,width="",0
            line+=char
            width+=size
        lines.append(line)
    return lines or [""]


@lru_cache(maxsize=8)
def _layout_font(size):
    from PIL import ImageFont
    path=FONT_FILE
    try:
        return ImageFont.truetype(str(path),round(size*4))
    except OSError:
        return None


def wrap_points(text, width, size):
    """실제 글꼴 폭으로 나눈다. 긴 URL/영문도 표의 줄 높이 계산과 일치시킨다."""
    font=_layout_font(size)
    if font is None:
        return wrap_text(text,max(2,int(width/size)))
    widths={char:font.getlength(char)/4 for char in set(plain(text))}
    lines=[]
    for original in plain(text).split('\n'):
        line,used='',0
        for char in original:
            advance=widths[char]
            if line and used+advance>width:
                lines.append(line)
                line,used='',0
            line+=char
            used+=advance
        lines.append(line)
    return lines or ['']


def render_docx(path, text, title, images=()):
    from docx import Document
    from docx.shared import Inches, Pt, RGBColor
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.enum.table import WD_TABLE_ALIGNMENT, WD_CELL_VERTICAL_ALIGNMENT
    from docx.enum.section import WD_ORIENT
    document=Document()
    section=document.sections[0]
    content=blocks(text)
    if any(block[0]=='table' and len(block[1])>5 for block in content):
        section.orientation=WD_ORIENT.LANDSCAPE
        section.page_width,section.page_height=section.page_height,section.page_width
    section.top_margin=section.bottom_margin=Inches(.75)
    section.left_margin=section.right_margin=Inches(.75)
    for name,size in (("Normal",11),("Title",22),("Heading 1",16),("Heading 2",13),("Heading 3",12)):
        style=document.styles[name]
        style.font.name=FONT
        style.font.size=Pt(size)
        style.font.color.rgb=RGBColor(0,0,0)
        style.element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"),FONT)
        style.paragraph_format.space_after=Pt(7)
    document.styles['Normal'].paragraph_format.line_spacing=1.16
    document.add_paragraph(plain(title),style='Title')
    width=section.page_width-section.left_margin-section.right_margin
    def add_runs(paragraph,value,size=None,bold=False,white=False):
        for piece in re.split(r'(\*\*.+?\*\*)',value):
            if not piece:continue
            run=paragraph.add_run(plain(piece))
            run.bold=bold or (piece.startswith('**') and piece.endswith('**'))
            if size:run.font.size=Pt(size)
            if white:run.font.color.rgb=RGBColor(255,255,255)
    for block in content:
        kind=block[0]
        if kind=='heading':
            if plain(block[2])!=plain(title): document.add_heading(plain(block[2]),block[1])
        elif kind=='table':
            header,rows=block[1:]
            table=document.add_table(rows=1,cols=len(header))
            table.alignment=WD_TABLE_ALIGNMENT.CENTER
            table.autofit=False
            table.style='Table Grid'
            borders=OxmlElement('w:tblBorders')
            for side in ('top','left','bottom','right','insideH','insideV'):
                border=OxmlElement('w:'+side)
                for key,value in (('val','single'),('sz','4'),('color','D9D9D9')):border.set(qn('w:'+key),value)
                borders.append(border)
            table._tbl.tblPr.append(borders)
            widths=[int(width*w) for w in column_weights(header,rows)]
            for col,w in zip(table.columns,widths): col.width=w
            for row_index,values in enumerate([header,*rows]):
                row=table.rows[0] if row_index==0 else table.add_row()
                if row_index==0:
                    repeat=OxmlElement('w:tblHeader');row._tr.get_or_add_trPr().append(repeat)
                for col_index,value in enumerate(values):
                    cell=row.cells[col_index];cell.width=widths[col_index]
                    cell.vertical_alignment=WD_CELL_VERTICAL_ALIGNMENT.CENTER
                    properties=cell._tc.get_or_add_tcPr()
                    shade=OxmlElement('w:shd');shade.set(qn('w:fill'),BLUE if row_index==0 else (PALE if row_index%2 else 'FFFFFF'));properties.append(shade)
                    margins=OxmlElement('w:tcMar')
                    for side in ('top','left','bottom','right'):
                        margin=OxmlElement('w:'+side);margin.set(qn('w:w'),'100');margin.set(qn('w:type'),'dxa');margins.append(margin)
                    properties.append(margins)
                    paragraph=cell.paragraphs[0];paragraph.paragraph_format.space_after=Pt(2);paragraph.paragraph_format.space_before=Pt(2)
                    add_runs(paragraph,value,10,row_index==0,row_index==0)
            document.add_paragraph().paragraph_format.space_after=Pt(3)
        else:
            paragraph=document.add_paragraph(style='List Bullet' if kind=='bullet' else 'Normal')
            add_runs(paragraph,block[1])
    for stream,w,h,source in _image_data(images):
        document.add_heading(T('doc_images'),1)
        document.add_picture(stream,width=Inches(min(6,4.8*w/max(1,h))))
        document.add_paragraph(plain(source))
    footer=section.footer.paragraphs[0]
    footer.alignment=2
    footer.add_run(T('doc_page_word'))
    field=OxmlElement('w:fldSimple');field.set(qn('w:instr'),'PAGE');footer._p.append(field)
    document.save(path)
    Document(path)


LINK = "1F5FBF"
INK = "1F2933"
MUTED = "5B6875"
INLINE = re.compile(r"\[((?:[^\[\]]|\[[^\]]*\])+)\]\(<?([^)>\s]+)>?\)|\*\*(.+?)\*\*|`([^`]+)`|(https?://[^\s<>()|\]]+)")


def _short_url(url, limit=48):
    """주소를 그대로 길게 늘어놓지 않고 '호스트/경로…'로 줄여 보인다(누르면 전체 주소로 이동)."""
    shown = re.sub(r"^https?://(?:www\.)?", "", url)
    return shown if len(shown) <= limit else shown[:limit-1] + "…"


def _pdf_inline(text):
    """마크다운 한 줄을 PDF 문단 표기로 바꾼다. 원문의 HTML은 실행/해석하지 않고 허용한 강조·링크만 새로 만든다."""
    from reportlab.pdfbase import pdfmetrics
    primary=pdfmetrics.getFont('BrowserKorean').face.charToGlyph
    symbols=pdfmetrics.getFont('BrowserSymbols').face.charToGlyph if 'BrowserSymbols' in pdfmetrics.getRegisteredFontNames() else {}
    def escaped(value):
        result=[]
        for char in plain(value):
            if char=='\n':result.append('<br/>')
            elif char in {'\ufe0e','\ufe0f'}:continue  # PDF에서는 단색 기호 글꼴을 사용한다.
            elif ord(char) not in primary and ord(char) in symbols:
                result.append('<font name="BrowserSymbols">'+html.escape(char)+'</font>')
            else:result.append(html.escape(char))
        return ''.join(result)
    def link(url, label):
        if not re.match(r'^(?:https?|file)://', url):
            return escaped(label) + (' (' + escaped(url) + ')' if url != label else '')
        return f'<link href="{html.escape(url, quote=True)}" color="#{LINK}"><u>{escaped(label)}</u></link>'
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    result, last = [], 0
    for match in INLINE.finditer(text):
        result.append(escaped(text[last:match.start()]))
        label, url, bold, code, bare = match.groups()
        if url:
            result.append(link(url, label))
        elif bold is not None:
            result.append('<b>' + _pdf_inline(bold) + '</b>')
        elif code is not None:
            result.append(f'<span backColor="#EEF1F5">&nbsp;{escaped(code)}&nbsp;</span>')
        else:
            result.append(link(bare, _short_url(bare)))
        last = match.end()
    result.append(escaped(text[last:]))
    return ''.join(result) or ' '


def render_pdf(path,text,title,images=(),theme=None):
    """보고서 PDF: 제목 띠, 단계별 제목·책갈피, 누르면 열리는 링크, 머리글 색 표, 인용 상자, 쪽 머리말·꼬리말."""
    from datetime import datetime
    from reportlab.lib.pagesizes import A4,landscape
    from reportlab.lib.colors import HexColor,white
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import SimpleDocTemplate,Paragraph,Spacer,LongTable,Table,TableStyle,PageBreak,Image,HRFlowable
    from pypdf import PdfReader
    font,bold=FONT_FILE,FONT_BOLD
    if not font.is_file():raise RuntimeError(T('no_font',name=font.name))
    if 'BrowserKorean' not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont('BrowserKorean',str(font)))
        pdfmetrics.registerFont(TTFont('BrowserKoreanBold',str(bold if bold.is_file() else font)))
        pdfmetrics.registerFontFamily('BrowserKorean',normal='BrowserKorean',bold='BrowserKoreanBold')
    symbols=font.with_name('seguisym.ttf')
    if symbols.is_file() and 'BrowserSymbols' not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont('BrowserSymbols',str(symbols)))
        pdfmetrics.registerFontFamily('BrowserSymbols',normal='BrowserSymbols',bold='BrowserSymbols')
    content=blocks(text)
    pagesize=landscape(A4) if any(b[0]=='table' and len(b[1])>5 for b in content) else A4
    width=pagesize[0]-84
    if theme is None:
        theme=("기본", {"color": BLUE})
    theme_name,theme_def=theme
    ink,muted,blue,pale=HexColor('#'+INK),HexColor('#'+MUTED),HexColor('#'+theme_def.get('color',BLUE)),HexColor('#'+PALE)
    body=ParagraphStyle('body',fontName='BrowserKorean',fontSize=10.2,leading=16,wordWrap='CJK',spaceAfter=6,textColor=ink)
    h1=ParagraphStyle('h1',parent=body,fontName='BrowserKoreanBold',fontSize=16,leading=22,spaceBefore=16,spaceAfter=2,textColor=blue,keepWithNext=True)
    h2=ParagraphStyle('h2',parent=h1,fontSize=13.5,leading=19,spaceBefore=12)
    h3=ParagraphStyle('h3',parent=h1,fontSize=11.5,leading=16,spaceBefore=10,spaceAfter=4,textColor=ink)
    bullet=ParagraphStyle('bullet',parent=body,leftIndent=16,bulletIndent=4,spaceAfter=3)
    number=ParagraphStyle('number',parent=bullet,leftIndent=20)
    note=ParagraphStyle('note',parent=body,fontSize=9.2,leading=14,textColor=muted,backColor=HexColor('#F5F7FA'),
                        leftIndent=10,rightIndent=4,borderPadding=(2,6,2,6),spaceAfter=0)
    cell=ParagraphStyle('cell',parent=body,fontSize=9,leading=13,spaceAfter=0)
    headcell=ParagraphStyle('headcell',parent=cell,fontName='BrowserKoreanBold',textColor=white)
    cover_title=ParagraphStyle('cover',parent=body,fontName='BrowserKoreanBold',fontSize=19,leading=26,textColor=white,spaceAfter=0)
    cover_meta=ParagraphStyle('covermeta',parent=body,fontSize=9,leading=13,textColor=HexColor('#D6E2EE'),spaceAfter=0)
    made=datetime.now().strftime('%Y-%m-%d %H:%M')
    band=Table([[Paragraph(_pdf_inline(title),cover_title)],[Paragraph(f'{brand()} · ' + T('doc_made', made=made),cover_meta)]],colWidths=[width])
    band.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),blue),('LEFTPADDING',(0,0),(-1,-1),14),('RIGHTPADDING',(0,0),(-1,-1),14),
                              ('TOPPADDING',(0,0),(0,0),14),('BOTTOMPADDING',(0,-1),(-1,-1),12),('TOPPADDING',(0,1),(0,1),4)]))
    flow=[band,Spacer(1,14)]
    notes=[]
    def flush_notes():
        # 연속된 인용(수집 원문 발췌)은 회색 상자처럼 이어 붙이고, 쪽이 넘어가면 나뉘게 둔다.
        if notes:
            flow.extend(Paragraph(_pdf_inline(line),note) for line in notes)
            flow.append(Spacer(1,8))
            notes.clear()
    for block in content:
        if block[0]!='note':flush_notes()
        if block[0]=='heading':
            if plain(block[2])==plain(title):continue
            level=block[1]
            flow.append(Paragraph(_pdf_inline(block[2]),{1:h1,2:h2}.get(level,h3)))
            if level<=2:
                flow.append(HRFlowable(width='100%',thickness=1.2 if level==1 else .6,color=blue if level==1 else HexColor('#B8C7D6'),
                                       spaceBefore=2,spaceAfter=8))
        elif block[0]=='table':
            header,rows=block[1:]
            data=[[Paragraph(_pdf_inline(value),headcell if i==0 else cell) for value in row] for i,row in enumerate([header,*rows])]
            table=LongTable(data,colWidths=[width*w for w in column_weights(header,rows)],repeatRows=1,splitInRow=1,hAlign='CENTER')
            table.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),blue),('ROWBACKGROUNDS',(0,1),(-1,-1),[white,pale]),
                                       ('GRID',(0,0),(-1,-1),.4,HexColor('#D9D9D9')),('VALIGN',(0,0),(-1,-1),'MIDDLE'),('NOSPLIT',(0,0),(-1,min(2,len(data)-1))),
                                       ('LEFTPADDING',(0,0),(-1,-1),7),('RIGHTPADDING',(0,0),(-1,-1),7),
                                       ('TOPPADDING',(0,0),(-1,-1),6),('BOTTOMPADDING',(0,0),(-1,-1),6)]))
            flow += [table,Spacer(1,12)]
        elif block[0]=='note':
            notes.append(block[1])
        elif block[0]=='bullet':
            flow.append(Paragraph(_pdf_inline(block[1]),bullet,bulletText='•'))
        else:
            numbered=re.match(r'^(\d+[.)])\s+(.*)',block[1])
            if numbered:flow.append(Paragraph(_pdf_inline(numbered[2]),number,bulletText=numbered[1]))
            else:flow.append(Paragraph(_pdf_inline(block[1]),body))
    flush_notes()
    for stream,w,h,source in _image_data(images):
        scale=min(width/max(1,w),420/max(1,h))
        flow += [PageBreak(),Paragraph(T('doc_images'),h2),Image(stream,w*scale,h*scale),Spacer(1,10),Paragraph(_pdf_inline(source),body)]
    short=plain(title)[:60]
    def decorate(canvas,document):
        canvas.saveState()
        canvas.setStrokeColor(HexColor('#D0D9E2'));canvas.setLineWidth(.5)
        canvas.line(42,30,pagesize[0]-42,30)
        canvas.setFont('BrowserKorean',8);canvas.setFillColor(muted)
        canvas.drawString(42,19,short)
        canvas.drawRightString(pagesize[0]-42,19,T('doc_page', n=document.page))
        if document.page>1:
            canvas.line(42,pagesize[1]-26,pagesize[0]-42,pagesize[1]-26)
            canvas.drawString(42,pagesize[1]-22,brand())
        canvas.restoreState()
    class Report(SimpleDocTemplate):
        outline_level=-1
        def afterFlowable(self, flowable):
            # PDF 보기 프로그램의 책갈피(목차)에 제목 단계를 넣는다.
            name=getattr(getattr(flowable,'style',None),'name','')
            if isinstance(flowable,Paragraph) and name in ('h1','h2','h3'):
                key=f'h{id(flowable)}'
                level=min({'h1':0,'h2':1,'h3':2}[name],self.outline_level+1)
                self.canv.bookmarkPage(key)
                self.canv.addOutlineEntry(flowable.getPlainText()[:80],key,level=level,closed=False)
                self.outline_level=level
    Report(str(path),pagesize=pagesize,leftMargin=42,rightMargin=42,topMargin=40,bottomMargin=44,
           title=plain(title),author=config.app_name()).build(flow,onFirstPage=decorate,onLaterPages=decorate)
    if not PdfReader(path).pages:raise RuntimeError(T('no_pages_made'))


def render_pptx(path, text, title, images=()):
    from pptx import Presentation
    from pptx.util import Inches, Pt
    from pptx.dml.color import RGBColor

    ppt = Presentation()
    ppt.slide_width, ppt.slide_height = Inches(13.333), Inches(7.5)
    ppt.core_properties.title = plain(title)
    heading_counts = {}
    current, cursor = None, 1.55
    section = plain(title)
    bottom = 6.72

    def paragraph(frame, value, size=18, bold=False, color='17243B', after=0):
        p = frame.paragraphs[0] if not frame.paragraphs[0].text else frame.add_paragraph()
        p.text = plain(value)
        p.font.name, p.font.size, p.font.bold = FONT, Pt(size), bold
        p.font.color.rgb = RGBColor.from_string(color)
        p.space_after = Pt(after)
        return p

    def new_slide(heading):
        nonlocal current, cursor
        current = ppt.slides.add_slide(ppt.slide_layouts[6])
        cursor = 1.55
        heading_counts[heading] = heading_counts.get(heading, 0) + 1
        number = heading_counts[heading]
        label = heading + (' · ' + T('doc_continued', n=number) if number > 1 else '')
        frame = current.shapes.add_textbox(Inches(.6), Inches(.3), Inches(12.1), Inches(1.15)).text_frame
        frame.word_wrap = True
        size=26 if len(label)<65 else 20
        paragraph(frame, '\n'.join(wrap_points(label, 850, size)), size, True)
        foot = current.shapes.add_textbox(Inches(.6), Inches(7.05), Inches(12), Inches(.25)).text_frame
        paragraph(foot, f'{config.app_name()}   |   {len(ppt.slides)}', 9, color='64748B')
        return current

    def add_text(value):
        nonlocal cursor
        lines = wrap_points(value, 845, 18)
        while lines:
            if current is None or bottom-cursor < .5:
                new_slide(section)
            count = max(1, min(len(lines), int((bottom-cursor-.12)/.32)))
            piece, lines = lines[:count], lines[count:]
            height = count*.32 + .12
            frame = current.shapes.add_textbox(Inches(.65), Inches(cursor), Inches(12), Inches(height)).text_frame
            frame.word_wrap = True
            paragraph(frame, '\n'.join(piece))
            cursor += height + .08

    def add_table(header, rows):
        nonlocal cursor
        # 넓은 표는 식별 열을 반복해 나눈다. 긴 셀은 이어지는 행으로 값을 보존한다.
        groups = ([list(range(len(header)))] if len(header) <= 5 else
                  [[0, *range(i, min(i+4, len(header)))] for i in range(1, len(header), 4)])
        for group_index, columns in enumerate(groups):
            if group_index:
                new_slide(section)
            heads = [header[i] for i in columns]
            data = [[row[i] for i in columns] for row in rows]
            widths = [12*w for w in column_weights(heads, data)]
            cell_widths = [w*72-14 for w in widths]
            heads = ['\n'.join(wrap_points(value, width, 14)) for value, width in zip(heads, cell_widths)]
            header_height = max(.55, max(len(value.splitlines()) for value in heads)*14*1.27/72+.19)
            physical = []
            for row in data:
                wrapped = [wrap_points(value, width, 14) for value, width in zip(row, cell_widths)]
                for offset in range(0, max(len(value) for value in wrapped), 6):
                    cells = ['\n'.join(value[offset:offset+6]) for value in wrapped]
                    if offset and not cells[0]:
                        cells[0] = wrap_points(plain(row[0])+' '+T('doc_cont_cell'),cell_widths[0],14)[0]
                    lines = max(len(value[offset:offset+6]) for value in wrapped)
                    physical.append((cells, max(.42, lines*14*1.27/72+.19)))
            if not physical:
                physical = [(['']*len(heads), .42)]
            while physical:
                if current is None or bottom-cursor < header_height+physical[0][1]:
                    new_slide(section)
                page, height = [], header_height
                while physical and (not page or cursor+height+physical[0][1] <= bottom):
                    cells, row_height = physical.pop(0)
                    page.append((cells, row_height))
                    height += row_height
                table = current.shapes.add_table(len(page)+1, len(heads), Inches(.65), Inches(cursor), Inches(12), Inches(height)).table
                for col, width in zip(table.columns, widths):
                    col.width = Inches(width)
                for ri, (values, row_height) in enumerate([(heads, header_height), *page]):
                    table.rows[ri].height = Inches(row_height)
                    for ci, value in enumerate(values):
                        cell = table.cell(ri, ci)
                        cell.margin_left = cell.margin_right = Inches(.09)
                        cell.margin_top = cell.margin_bottom = Inches(.07)
                        cell.fill.solid()
                        cell.fill.fore_color.rgb = RGBColor.from_string(BLUE if ri == 0 else (PALE if ri % 2 else 'FFFFFF'))
                        cell.text_frame.word_wrap = True
                        paragraph(cell.text_frame, value, 14, ri == 0, 'FFFFFF' if ri == 0 else '17243B')
                cursor += height + .18
                if physical:
                    new_slide(section)

    for block in blocks(text):
        if block[0] == 'heading':
            section = plain(block[2])
            current = None
        elif block[0] == 'table':
            add_table(block[1], block[2])
        else:
            add_text(('• ' if block[0] == 'bullet' else '')+block[1])
    for stream, w, h, source in _image_data(images):
        slide = new_slide(T('doc_images'))
        scale = min(11/max(1, w), 4.6/max(1, h))
        width, height = w*scale, h*scale
        slide.shapes.add_picture(stream, Inches((13.333-width)/2), Inches(1.45), width=Inches(width), height=Inches(height))
        frame = slide.shapes.add_textbox(Inches(.65), Inches(6.25), Inches(12), Inches(.65)).text_frame
        frame.word_wrap = True
        caption=wrap_points(source,845,11)
        paragraph(frame, '\n'.join(caption[:2]), 11)
        if len(caption)>2:
            section=T('doc_images_cont')
            current=None
            add_text('\n'.join(caption[2:]))
    if not ppt.slides:
        new_slide(title)
    ppt.save(path)
    Presentation(path)


# --- 저장 (browser_reports.py 의 export_report·_image_data·_write) -----------------------------

FORMATS = ("pdf", "docx", "pptx", "md", "txt")
MAX_IMAGES = 6
_IMAGE_LINE = re.compile(r"!\[([^]]*)\]\(<?([^)>]+)>?\)")


def _image_data(images):
    """images: [{"path": 경로, "source": 설명}] 또는 경로 글 목록."""
    from PIL import Image
    seen = set()
    for item in images:
        item = item if isinstance(item, dict) else {"path": str(item)}
        path = Path(item["path"])
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        with Image.open(path) as image:
            if image.width * image.height > 25000000:
                raise ValueError(T("image_too_large"))
            image.thumbnail((1400, 1000))
            stream = io.BytesIO()
            rgba = image.convert("RGBA")   # 투명한 곳은 흰 바탕으로(원본은 검게 나왔다)
            flat = Image.new("RGB", rgba.size, "white")
            flat.paste(rgba, mask=rgba.getchannel("A"))
            flat.save(stream, format="PNG")
            stream.seek(0)
            yield stream, image.width, image.height, item.get("source") or path.name
        if len(seen) >= MAX_IMAGES:
            break


def local_images(text, base=None):
    """마크다운 안의 ![설명](내 PC 그림 경로) 를 첨부 그림 목록으로. 인터넷 주소는 넣지 않는다."""
    found = []
    for alt, target in _IMAGE_LINE.findall(text):
        if re.match(r"^[a-z]+://", target, re.I) and not target.lower().startswith("file://"):
            continue
        path = Path(re.sub(r"^file:///?", "", target, flags=re.I))
        if not path.is_absolute() and base:
            path = Path(base) / path
        if path.is_file():
            found.append({"path": str(path), "source": alt or path.name})
    return found


def short_name(title, limit=30):
    name = re.sub(r'[<>:"/\|?*\x00-\x1f]', "_", plain(title)).strip(" ._")
    return re.sub(r"\s+", "_", name)[:limit].rstrip("_") or T("untitled")


def export(text, title, folder, formats=("pdf",), images=(), now=None):
    """마크다운을 형식별 파일로 저장한다. 이름은 YYYYMMDD-HHMM_제목(같은 이름이 있으면 초까지, 그래도 있으면 _2…).
    돌려주는 값: {"saved": [경로], "failed": ["형식: 이유"]}"""
    formats = tuple(dict.fromkeys(str(k).lower().lstrip(".") for k in formats))
    if not formats or any(kind not in FORMATS for kind in formats):
        raise ValueError(T("supported_formats", formats=", ".join(FORMATS)))
    folder = Path(folder).expanduser()
    if not folder.is_absolute():
        raise ValueError(T("need_folder"))
    folder.mkdir(parents=True, exist_ok=True)
    now = now or datetime.now()
    name = short_name(title)
    images = list(images) or local_images(text)
    result = {"saved": [], "failed": []}
    for kind in formats:
        candidates = [folder / f"{now:%Y%m%d-%H%M}_{name}.{kind}", folder / f"{now:%Y%m%d-%H%M%S}_{name}.{kind}"]
        candidates += [folder / f"{now:%Y%m%d-%H%M%S}_{name}_{i}.{kind}" for i in range(2, 100)]
        temp = folder / f"~{os.getpid()}_{time.monotonic_ns()}.tmp.{kind}"
        try:
            if kind in ("md", "txt"):
                body = text if re.match(r"^\s*#\s", text) else f"# {title}\n\n{text}"
                temp.write_text(body, encoding="utf-8")
            else:
                {"pdf": render_pdf, "docx": render_docx, "pptx": render_pptx}[kind](temp, text, title, images)
            if not temp.is_file() or not temp.stat().st_size:
                raise RuntimeError(T("empty_file"))
            for target in candidates:
                try:
                    os.link(temp, target)   # 있는 파일은 덮어쓰지 않는다
                    break
                except FileExistsError:
                    continue
            else:
                raise RuntimeError(T("too_many_names"))
            result["saved"].append(str(target))
        except Exception as exc:
            result["failed"].append(f"{kind}: {exc}")
        finally:
            temp.unlink(missing_ok=True)
    return result


# --- PDF 글 뽑기 (control/pdf_reader.py 의 read_pages) --------------------------------------------

MAX_CHARS = 30_000          # 한 번에 돌려줄 글자 수
MAX_PAGES = 60              # 한 번에 읽을 쪽 수
MAX_BYTES = 50 * 1024 * 1024
TIME_LIMIT = 30.0           # 한 번 읽기의 시간 상한(초)
MIN_CHARS_PER_PAGE = 20     # 쪽당 평균 글자가 이보다 적으면 스캔본으로 본다
UNTRUSTED_NOTE = T("untrusted")


class ReadError(Exception):
    pass


def _clean(text: str) -> str:
    text = re.sub(r"[ \t　]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def read_pdf(path, start: int = 0, end: int = 0, clock=time.monotonic) -> dict:
    """start~end 쪽(1부터, 0이면 처음·끝까지) 글을 한도 안에서 뽑는다.
    돌려주는 값: {"name", "total", "first", "last", "chars", "pages": [(쪽, 글)], "notes": [안내]}"""
    path = Path(path)
    if not path.is_file():
        raise ReadError(T("no_file", path=path))
    size = path.stat().st_size
    if size > MAX_BYTES:
        raise ReadError(T("file_too_large", size=size / 1048576, limit=MAX_BYTES // 1048576))
    try:
        from pypdf import PdfReader
        reader = PdfReader(path)
        if getattr(reader, "is_encrypted", False):
            try:
                if not reader.decrypt(""):
                    raise ReadError(T("encrypted"))
            except ReadError:
                raise
            except Exception:
                raise ReadError(T("encrypted")) from None
        total = len(reader.pages)
    except ReadError:
        raise
    except Exception:
        raise ReadError(T("cannot_open")) from None
    if total == 0:
        raise ReadError(T("zero_pages"))
    first = start if start > 0 else 1
    if first > total:
        raise ReadError(T("page_missing", total=total, first=first))
    last = min(end if end > 0 else total, total, first + MAX_PAGES - 1)
    if last < first:
        raise ReadError(T("bad_range", start=start, end=end, total=total))
    began = clock()
    pages, chars, read_until = [], 0, first - 1
    clipped = stopped_for_time = False
    for number in range(first, last + 1):
        if clock() - began > TIME_LIMIT and pages:
            stopped_for_time = True
            break
        try:
            text = _clean(reader.pages[number - 1].extract_text() or "")
        except Exception:
            text = ""
        room = MAX_CHARS - chars
        if len(text) > room:
            if pages:
                break
            text, clipped = text[:room], True
        pages.append((number, text))
        chars += len(text)
        read_until = number
        if clipped:
            break
    notes = []
    if chars < MIN_CHARS_PER_PAGE * (read_until - first + 1):
        notes.append(T("scanned"))
    if clipped:
        notes.append(T("clipped", page=read_until))
    if read_until < total:
        why = T("limit_time") if stopped_for_time else T("limit_once")
        notes.append(T("stopped", why=why, page=read_until, next=read_until + 1, total=total))
    return {"name": path.name, "total": total, "first": first, "last": read_until, "chars": chars, "pages": pages, "notes": notes}
