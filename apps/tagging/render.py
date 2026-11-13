"""What goes on a tag: the QR payload, the QR code and Code128 barcode as
inline SVG, and ZPL for Zebra-style label printers."""
import re

# Code128: bar/space widths of symbols 0..105, then STOP (106).
_C128 = (
    "212222 222122 222221 121223 121322 131222 122213 122312 132212 221213 "
    "221312 231212 112232 122132 122231 113222 123122 123221 223211 221132 "
    "221231 213212 223112 312131 311222 321122 321221 312212 322112 322211 "
    "212123 212321 232121 111323 131123 131321 112313 132113 132311 211313 "
    "231113 231311 112133 112331 132131 113123 113321 133121 313121 211331 "
    "231131 213113 213311 213131 311123 311321 331121 312113 312311 332111 "
    "314111 221411 431111 111224 111422 121124 121421 141122 141221 112214 "
    "112412 122114 122411 142112 142211 241211 221114 413111 241112 134111 "
    "111242 121142 121241 114212 124112 124211 411212 421112 421211 212141 "
    "214121 412121 111143 111341 131141 114113 114311 411113 411311 113141 "
    "114131 311141 411131 211412 211214 211232 2331112"
).split()
START_B, START_C, CODE_B, CODE_C, STOP = 104, 105, 100, 99, 106


def payload(tag, batch=None, company=None):
    """The text inside the QR: the code first, then what the tag is, on one
    line joined by " | " so a scanner typing it into a search box sends one
    line. Never a URL: the code itself is what the lookup searches for."""
    batch = batch or tag.batch
    if batch.qr_content == "CODE":
        return tag.code
    company = company or batch.company
    parts = [tag.code, tag.name, company.name, batch.subject_text, *batch.lines]
    clean = [re.sub(r"\s+", " ", str(p).replace("|", "/")).strip() for p in parts if p]
    return " | ".join(clean)[:400]


def code_from_scan(text):
    """What a scanner typed -> the tag code: the first " | " part of a QR."""
    return (text or "").split("|")[0].strip()


def qr_svg(value, label="QR code"):
    """An inline SVG QR code, black on white, filling its box."""
    import qrcode
    import qrcode.image.svg
    img = qrcode.make(str(value), image_factory=qrcode.image.svg.SvgPathImage, border=1,
                      error_correction=qrcode.constants.ERROR_CORRECT_M)
    svg = img.to_string(encoding="unicode")
    svg = re.sub(r"^<\?xml[^>]*\?>\s*", "", svg)
    view = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg)
    w, h = (view.group(1), view.group(2)) if view else ("100%", "100%")
    svg = re.sub(r'\s(width|height)="[^"]*"', "", svg, count=2)
    svg = svg.replace("<svg ", f'<svg class="qr" role="img" aria-label="{label}" shape-rendering="crispEdges" '
                               'preserveAspectRatio="xMidYMid meet" ', 1)
    return re.sub(r"(<svg[^>]*>)", rf'\1<rect width="{w}" height="{h}" fill="#ffffff"/>', svg, count=1)


def qr_png(value, box=4):
    """The same QR as a PIL image (the tests decode it)."""
    import qrcode
    q = qrcode.QRCode(border=4, box_size=box, error_correction=qrcode.constants.ERROR_CORRECT_M)
    q.add_data(str(value))
    q.make(fit=True)
    return q.make_image(fill_color="black", back_color="white").get_image()


def code128_values(text):
    """Symbol values for `text` (ASCII 32..126): code set C for runs of four or
    more digits (and an even run that ends the text), B otherwise; check
    symbol included, STOP left off."""
    if not text or any(not 32 <= ord(ch) <= 126 for ch in text):
        raise ValueError("Code128 here takes printable ASCII only.")
    vals, i, cur = [], 0, None

    def digits_at(j):
        k = j
        while k < len(text) and text[k].isdigit():
            k += 1
        return k - j

    while i < len(text):
        run = digits_at(i)
        if run >= 4:
            run -= run % 2
            if cur != "C":
                vals.append(START_C if cur is None else CODE_C)
                cur = "C"
            for j in range(i, i + run, 2):
                vals.append(int(text[j:j + 2]))
            i += run
        else:
            if cur != "B":
                vals.append(START_B if cur is None else CODE_B)
                cur = "B"
            vals.append(ord(text[i]) - 32)
            i += 1
    check = (vals[0] + sum(v * n for n, v in enumerate(vals[1:], 1))) % 103
    return vals + [check]


def code128_modules(text):
    """The barcode as 1 (bar) / 0 (space) modules, quiet zones left off."""
    out = []
    for v in code128_values(text) + [STOP]:
        for k, width in enumerate(_C128[v]):
            out.append(("1" if k % 2 == 0 else "0") * int(width))
    return "".join(out)


def code128_svg(text, quiet=10):
    mods = code128_modules(text)
    width = len(mods) + 2 * quiet
    rects = [f'<rect x="{quiet + m.start()}" y="0" width="{len(m.group())}" height="40"/>'
             for m in re.finditer(r"1+", mods)]
    return (f'<svg class="bc" viewBox="0 0 {width} 40" preserveAspectRatio="none" role="img" '
            f'aria-label="Barcode {text}" shape-rendering="crispEdges"><rect width="{width}" height="40" '
            f'fill="#fff"/><g fill="#000">{"".join(rects)}</g></svg>')


def _zpl_text(s):
    return re.sub(r"[\^~\\]", " ", str(s))


def zpl(tags, batch, company, copies=1, price=""):
    """ZPL II (203 dpi, 8 dots/mm) for each tag: QR on the left; company,
    code, details and an optional Code128 on the right. No logo: thermal
    printers want it stored on the printer as a graphic first."""
    lw, lh = batch.size[5], batch.size[6]
    w, h = int(lw * 8), int(lh * 8)
    qr_mag = max(1, min(10, int(h / 40)))
    x_text = 12 + (int(h * 0.9) if batch.show_qr else 0)
    font = max(16, int(h / 9))
    out = []
    for tag in tags:
        rows = [f"^XA^CI28^PW{w}^LL{h}^LH0,0"]
        if copies > 1:
            rows.append(f"^PQ{copies}")
        if batch.show_qr:
            rows.append(f"^FO8,8^BQN,2,{qr_mag}^FDMA,{_zpl_text(payload(tag, batch, company))}^FS")
        y = 10
        if batch.show_company:
            rows.append(f"^FO{x_text},{y}^A0N,{font},{font}^FD{_zpl_text(company.name)}^FS")
            y += font + 4
        if batch.show_code:
            big = int(font * 1.4)
            rows.append(f"^FO{x_text},{y}^A0N,{big},{big}^FD{_zpl_text(tag.code)}^FS")
            y += big + 4
        if tag.name:
            rows.append(f"^FO{x_text},{y}^A0N,{font},{font}^FD{_zpl_text(tag.name)}^FS")
            y += font + 4
        small = int(font * .85)
        for line in ([batch.subject_text] if batch.subject_text else []) + batch.lines + ([price] if price else []):
            rows.append(f"^FO{x_text},{y}^A0N,{small},{small}^FD{_zpl_text(line)}^FS")
            y += small + 3
        if batch.show_barcode and y < h - 30:
            rows.append(f"^FO{x_text},{y + 2}^BY1^BCN,{max(20, h - y - 12)},N,N,N^FD{_zpl_text(tag.code)}^FS")
        rows.append("^XZ")
        out.append("".join(rows))
    return "\n".join(out) + "\n"
