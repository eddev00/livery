// wordeasy template — all styling as set/show rules. The content file that is
// included by main.typ is plain pandoc-generated Typst and never contains styling.
//
// sys.inputs understood:
//   theme  = root-relative path to a theme.toml (default: the one beside this file)
//   cover  = name of a [cover.variants.*] table in the theme; overrides [cover].variant
//   verify = "true" to produce the bare render used by the content verifier:
//            no cover, no TOC, no header/footer, no chapter pages, no numbering,
//            hyphenation/ligatures off, wide word spacing (pdftotext must never
//            glue two words together).

#let _len(s) = eval(s)
#let _weight(w) = if type(w) == int { w } else { w }
// A color is a hex string or the name of an entry in [colors].
#let _color(t, s) = if s.starts-with("#") { rgb(s) } else { rgb(t.colors.at(s)) }

#let load-theme() = {
  let path = sys.inputs.at("theme", default: "theme.toml")
  toml(path)
}

#let project(
  title: "",
  department: none,
  department_name: none,
  version: none,
  number: none,
  cover: none,
  body,
) = {
  let t = load-theme()
  let doc-info = t.at("document", default: (:))
  let dept = if department == none { doc-info.at("department", default: "") } else { department }
  let dept-name = if department_name == none { doc-info.at("department_name", default: "") } else { department_name }
  let doc-version = if version == none { doc-info.at("version", default: "") } else { version }
  let verify = sys.inputs.at("verify", default: "false") == "true"

  let accent = _color(t, t.colors.accent)
  let accent-soft = _color(t, t.colors.accent_soft)
  let muted = _color(t, t.colors.muted)
  let rule = _color(t, t.colors.rule)
  let text-color = _color(t, t.colors.text)
  let body-style = t.text.body
  let body-size = _len(body-style.size)
  let body-leading = _len(body-style.leading)
  // Applies one [text.*] style; used for headings (block spacing = Word before/after).
  let heading-style(name, it) = {
    let st = t.text.at(name)
    set text(font: t.fonts.heading, size: _len(st.size), weight: _weight(st.weight), fill: _color(t, st.color), hyphenate: false)
    // Word's before/after spacing sits on top of the normal line pitch (= leading in Typst).
    block(above: _len(st.space_before) + body-leading, below: _len(st.space_after) + body-leading, it)
  }
  let numbering-depth = t.headings.at("numbering_depth", default: 3)
  let include-chapter = t.headings.at("include_chapter_number", default: true)
  let numbering-pattern = if t.headings.numbering == "" { none } else {
    (..nums) => {
      let n = nums.pos()
      if n.len() > numbering-depth {
        none
      } else if include-chapter {
        numbering(t.headings.numbering, ..n)
      } else if n.len() == 1 {
        none  // the chapter number is shown on the chapter page instead
      } else {
        numbering(t.headings.numbering, ..n.slice(1))
      }
    }
  }

  // ---------------------------------------------------------------- document
  set document(title: title)

  // Footer on every page: document title on the left, page number on the right.
  // It spans from `footer_side_margin` off the left page edge to the same off the
  // right edge, whatever the page's own margins are (ml/mr = that page's margins).
  let footer-side = _len(t.page.at("footer_side_margin", default: t.page.margin_left))
  let footer-at(ml, mr, body) = pad(left: footer-side - ml, right: footer-side - mr, body)
  let footer-content = {
    let hs = t.text.header
    set text(font: t.fonts.heading, size: _len(hs.size), weight: _weight(hs.weight), fill: _color(t, hs.color))
    let left-part = if dept == "" { title } else { [#strong(dept) #h(0.5em) #title] }
    // Pass the total only when the pattern has a second slot ("1 / 1").
    let both = t.page.number_format.matches(regex("[1aAiI]")).len() > 1
    grid(columns: (1fr, auto), align: (left, right), left-part, strong(context counter(page).display(t.page.number_format, both: both)))
  }

  // -------------------------------------------------------------------- page
  set page(
    paper: t.page.paper,
    margin: (
      top: _len(t.page.margin_top),
      bottom: _len(t.page.margin_bottom),
      left: _len(t.page.margin_left),
      right: _len(t.page.margin_right),
    ),
    header: if verify or not t.page.running_header { none } else {
      context {
        // No running header on the cover or on chapter separator pages.
        let here-page = counter(page).get().first()
        let chapter-pages = query(<wordeasy-chapter-page>).map(m => counter(page).at(m.location()).first())
        if here-page > 1 and not chapter-pages.contains(here-page) {
          let hs = t.text.header
          set text(font: t.fonts.heading, size: _len(hs.size), weight: _weight(hs.weight), fill: _color(t, hs.color))
          // Current chapter = last level-1 heading before this point. Pages
          // before the first chapter (contents, front matter) get no header.
          let chapters = query(selector(heading.where(level: 1)).before(here()))
          if chapters.len() > 0 {
            let ch = chapters.last()
            let n = counter(heading).at(ch.location()).first()
            // Spans from `header_side_margin` off each page edge, whatever the margins.
            let side = _len(t.page.at("header_side_margin", default: t.page.margin_left))
            pad(left: side - _len(t.page.margin_left), right: side - _len(t.page.margin_right))[
              #block(width: 100%)[#strong(upper(t.headings.chapter_label) + " " + numbering(t.headings.chapter_number_format, n)) #h(0.6em) #ch.body]
              #if t.page.at("header_rule", default: false) {
                v(-0.6em)
                line(length: 100%, stroke: 0.4pt + rule)
              }
            ]
          }
        }
      }
    },
    footer: if verify or not t.page.page_numbers { none } else {
      footer-at(_len(t.page.margin_left), _len(t.page.margin_right), footer-content)
    },
  )

  // -------------------------------------------------------------------- text
  set text(
    font: t.fonts.body,
    size: body-size,
    weight: _weight(body-style.weight),
    fill: _color(t, body-style.color),
    lang: t.typography.lang,
    hyphenate: if verify { false } else { t.typography.hyphenate },
    ligatures: if verify { false } else { t.typography.ligatures },
    spacing: if verify { 250% } else { 100% },
  )
  set par(
    justify: if verify { false } else { t.typography.justify },
    leading: body-leading,
    // Word "spacing after" is added on top of the normal line pitch.
    spacing: body-leading + _len(body-style.space_after) + _len(body-style.space_before),
    first-line-indent: _len(t.typography.first_line_indent),
  )

  // ---------------------------------------------------------------- headings
  set heading(numbering: if verify { none } else { numbering-pattern })
  show heading: set text(font: t.fonts.heading, hyphenate: false)
  show heading.where(level: 2): it => heading-style("h2", it)
  show heading.where(level: 3): it => heading-style("h3", it)
  show heading.where(level: 4): it => heading-style("h4", it)
  show heading.where(level: 5): it => heading-style("h4", it)
  show heading.where(level: 6): it => heading-style("h4", it)

  // Chapter separator page for every level-1 heading.
  show heading.where(level: 1): it => {
    if verify or not t.chapter_page.enabled {
      heading-style("h1", it)
    } else {
      pagebreak(weak: true)
      let cp = t.chapter_page
      let filled = cp.at("fill", default: "") != ""
      page(
        header: none,
        footer: if verify or not t.page.page_numbers { none } else { footer-at(0pt, 0pt, footer-content) },
        margin: (left: 0pt, right: 0pt, top: 0pt, bottom: _len(t.page.margin_bottom)),
        fill: if filled { _color(t, cp.fill) } else { none },
      )[
        #place(top + left, [#metadata("chapter") <wordeasy-chapter-page>])
        #if cp.at("background", default: "") != "" {
          context {
            // Aspect ratio is always kept: one dimension comes from the theme, the
            // other from the image's natural proportions, measured in an unbounded
            // space. Both are then set explicitly: an image wider than the page
            // would otherwise be laid out at the page width and cropped.
            // The free dimension is passed as `auto` explicitly, otherwise the
            // global `set image(width: ...)` rule for figures would apply to it.
            let probe = if "background_height" in cp { image(cp.background, height: eval(cp.background_height), width: auto) }
              else { image(cp.background, width: eval(cp.background_width), height: auto) }
            let sz = measure(probe, width: 1000cm, height: 1000cm)
            let img = image(cp.background, width: sz.width, height: sz.height)
            // Horizontally the image's centre goes to center_x from the left edge;
            // vertically its bottom edge sits `background_bottom` above the page bottom.
            let cx = eval(cp.background_center_x)
            let by = eval(cp.background_bottom)
            let x = if type(cx) == ratio { page.width * cx } else { cx }
            let y = if type(by) == ratio { page.height * by } else { by }
            place(top + left, dx: x - sz.width / 2, dy: page.height - y - sz.height, img)
          }
        }
        #if not filled and cp.band {
          place(top + left, rect(width: 1.8cm, height: 100%, fill: accent))
        }
        #let left-pad = if not filled and cp.band { 1.8cm + 2.5cm } else { 2.5cm }
        #let title-color = _color(t, cp.at("title_color", default: t.text.h1.color))
        #let number-color = _color(t, cp.at("number_color", default: t.text.h1.color))
        #place(
          bottom + left,
          dx: left-pad,
          dy: -_len(cp.at("block_bottom", default: "3cm")),
          block(width: 100% - left-pad - 2.5cm)[
            #set text(font: t.fonts.heading, hyphenate: false)
            #set par(justify: false)
            // Section number on its own line, then the section title.
            #if numbering-pattern != none and it.numbering != none [
              #text(
                font: cp.at("number_font", default: t.fonts.heading),
                weight: _weight(cp.at("number_weight", default: "bold")),
                stretch: eval(cp.at("number_stretch", default: "100%")),
                size: _len(cp.number_size), fill: number-color,
                counter(heading).display(t.headings.chapter_number_format),
              )
              #v(_len(cp.at("number_title_gap", default: "0.5em")))
            ]
            #text(font: cp.at("title_font", default: t.fonts.heading), size: _len(cp.title_size), fill: title-color, weight: _weight(t.text.h1.weight), it.body)
          ],
        )
      ]
    }
  }

  // ------------------------------------------------------------------ tables
  // Cell colours extracted from the Word file (see wordeasy/table_fills.py):
  // fills[table index][row][column] = hex or null. Tables are counted in order.
  let tables-data = if "fills" in sys.inputs { json(sys.inputs.fills) } else { () }
  let default-fill = _color(t, t.tables.default_fill)
  let grid-at(n, key, x, y) = {
    if n < tables-data.len() {
      let g = tables-data.at(n).at(key, default: ())
      if y < g.len() and x < g.at(y).len() { g.at(y).at(x) } else { none }
    } else { none }
  }
  // Rules keyed by the Word background colour ([[tables.cell_rules]] in the theme).
  let cell-rules = t.tables.at("cell_rules", default: ())
  let rule-for(v) = {
    let hits = cell-rules.filter(r => upper(r.word_fill) == upper(v))
    if hits.len() > 0 { hits.first() } else { none }
  }
  // Word colour of a cell, or none when unshaded (a cell explicitly shaded
  // with the default colour counts as unshaded).
  let word-fill(n, x, y) = {
    let v = grid-at(n, "fills", x, y)
    if v != none and upper(v) != upper(t.tables.default_fill) { v } else { none }
  }
  // Rendered text colour: the rule's text if any, else colored/default text.
  let cell-text(n, x, y) = {
    let v = word-fill(n, x, y)
    if v == none { _color(t, t.tables.default_text) } else {
      let r = rule-for(v)
      if r != none and "text" in r { _color(t, r.text) } else { _color(t, t.tables.colored_text) }
    }
  }
  // WCAG relative luminance and contrast ratio (1 = same, 21 = black on white).
  let luminance(c) = {
    let lin(v) = { let v = v / 100%; if v <= 0.03928 { v / 12.92 } else { calc.pow((v + 0.055) / 1.055, 2.4) } }
    let (r, g, b) = c.rgb().components(alpha: false)
    0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)
  }
  let contrast(a, b) = {
    let (la, lb) = (luminance(a), luminance(b))
    (calc.max(la, lb) + 0.05) / (calc.min(la, lb) + 0.05)
  }
  // A Word colour too close to the text colour (yellow under white text) is
  // pushed away from it until the contrast is reached: darker under light text,
  // lighter under dark text. Only the HSL lightness moves, in 5% steps; hue and
  // saturation stay, so a pale yellow becomes a deep yellow, not a grey.
  let min-contrast = float(t.tables.at("min_contrast", default: 1.5))
  let readable-fill(bg, fg) = {
    let (h, s, l) = bg.hsl().components(alpha: false)
    let step = if luminance(fg) > luminance(bg) { -5% } else { 5% }
    let c = bg
    let i = 0
    while contrast(c, fg) < min-contrast and i < 20 {
      i += 1
      c = color.hsl(h, s, calc.max(0%, calc.min(100%, l + i * step)))
    }
    c
  }
  // Rendered background: the rule's fill if any, else the Word colour (adjusted
  // for contrast with the text), else none.
  let cell-fill(n, x, y) = {
    let v = word-fill(n, x, y)
    if v == none { none } else {
      let r = rule-for(v)
      let bg = if r != none and "fill" in r { _color(t, r.fill) } else { rgb(v) }
      readable-fill(bg, cell-text(n, x, y))
    }
  }
  let h-align = (left: left, center: center, right: right).at(t.tables.at("align", default: "left"), default: left)
  let v-align-of(name) = (top: top, center: horizon, bottom: bottom).at(name, default: top)
  let cell-align(n, x, y) = {
    let mode = t.tables.at("valign", default: "word")
    let v = if mode == "word" { grid-at(n, "valign", x, y) } else { mode }
    h-align + v-align-of(if v == none { "top" } else { v })
  }
  // Global defaults (also cover tables that are not wrapped in a figure).
  let separator-line = if t.tables.at("separator_color", default: "") == "" { none } else {
    _len(t.tables.at("separator_width", default: "1.5pt")) + _color(t, t.tables.separator_color)
  }
  let separator = if separator-line == none { none } else {
    let line = separator-line
    let dir = t.tables.at("separator_direction", default: "horizontal")
    // Each cell draws its bottom and/or right edge: a line under every row (including
    // the last) but none above the first row; likewise none left of the first column.
    (
      top: none, left: none,
      bottom: if dir == "vertical" { none } else { line },
      right: if dir == "horizontal" { none } else { line },
    )
  }
  // A cell next to a vertically merged cell it does not share rows with (or a
  // merged cell next to single cells) also draws its left edge, so the row lines
  // that stop at the merged cell end on a stroke instead of in the open.
  let merge-edge(n, x, y) = x > 0 and grid-at(n, "rows", x - 1, y) != grid-at(n, "rows", x, y)
  let cell-stroke(n, x, y) = {
    if separator == none { none }
    else if merge-edge(n, x, y) { separator + (left: separator-line) }
    else { separator }
  }
  set table(
    stroke: separator,
    inset: (
      x: _len(t.tables.inset),
      top: _len(t.tables.at("inset_top", default: t.tables.inset)),
      bottom: _len(t.tables.at("inset_bottom", default: t.tables.inset)),
    ),
    fill: default-fill,
    align: if t.tables.at("align", default: "left") == "word" { auto } else { h-align },
  )
  // pandoc puts an explicit hline under the header row; an explicit line overrides
  // the cell edges there, so it must carry the separator stroke (or none).
  let dir = t.tables.at("separator_direction", default: "horizontal")
  set table.hline(stroke: if dir == "vertical" { none } else { separator-line })
  set table.vline(stroke: none)
  show table: set text(size: _len(t.tables.font_size))
  // Per-table colours: a set rule inside a `show table` rule never restyles that
  // same table, but the table is a child of the figure pandoc wraps it in, so the
  // figure's show rule can apply it. Tables are counted in document order.
  let table-counter = counter("wordeasy-table")
  show figure.where(kind: table): it => {
    table-counter.step()
    context {
      let n = table-counter.get().first() - 1
      set table(
        fill: (x, y) => { let c = cell-fill(n, x, y); if c == none { default-fill } else { c } },
        align: (x, y) => cell-align(n, x, y),
        stroke: (x, y) => cell-stroke(n, x, y),
      )
      it
    }
  }
  show table.cell: it => context {
    let n = table-counter.get().first() - 1
    set text(fill: cell-text(n, it.x, it.y))
    if t.tables.header_bold and it.y == 0 { strong(it) } else { it }
  }
  show figure.where(kind: table): set figure.caption(position: if t.tables.caption_position == "top" { top } else { bottom })
  show figure.where(kind: table): set block(breakable: true)

  // ----------------------------------------------------------------- figures
  show figure.where(kind: image): set figure.caption(position: if t.figures.caption_position == "top" { top } else { bottom })
  set image(width: _len(t.figures.max_width))
  show figure.caption: set text(size: _len(t.figures.at("caption_size", default: "10pt")), fill: _color(t, t.figures.at("caption_color", default: "#404040")), font: t.fonts.heading)
  show figure.caption: set align(center)
  show figure.where(kind: image): set align(center)
  show figure.where(kind: image): set figure(supplement: none, numbering: none)
  show figure.caption: it => {
    // Supplement ("Table 1:") is chrome; keep it out of the verify render.
    if verify { it.body } else { it }
  }

  // ------------------------------------------------------------ lists & misc
  let li = t.text.list
  let list-spacing = _len(li.leading) + _len(li.space_after)
  let marker-colors = li.at("marker_colors", default: ("brand_red", "#000000", "#000000"))
  let marker(c) = if li.at("marker_shape", default: "square") == "square" {
    box(baseline: -0.05em, square(size: _len(li.at("marker_size", default: "0.36em")), fill: _color(t, c)))
  } else {
    text(fill: _color(t, c), li.marker_shape)
  }
  set list(
    indent: _len(li.at("indent", default: "1em")),
    body-indent: _len(li.at("marker_gap", default: "0.6em")),
    marker: marker-colors.map(marker),
    spacing: list-spacing,
  )
  set enum(indent: _len(li.at("indent", default: "1em")), body-indent: _len(li.at("marker_gap", default: "0.6em")), spacing: list-spacing)
  show list: set text(size: _len(li.size), weight: _weight(li.weight), fill: _color(t, li.color))
  show enum: set text(size: _len(li.size), weight: _weight(li.weight), fill: _color(t, li.color))
  show list: set par(leading: _len(li.leading))
  show enum: set par(leading: _len(li.leading))
  show list.item: it => if verify { block(it.body) } else { it }
  show enum.item: it => if verify { block(it.body) } else { it }
  show footnote: it => if verify { [ #it.body ] } else { it }
  show footnote.entry: it => if verify { [] } else { it }
  set table.header(repeat: not verify and t.tables.at("repeat_header_rows", default: false))
  show super: it => if verify { it.body } else { it }
  show sub: it => if verify { it.body } else { it }
  show quote.where(block: true): it => block(
    width: 100%,
    inset: (left: 1.2em, right: 1em, top: 0.8em, bottom: 0.8em),
    stroke: (left: 3pt + accent),
    fill: accent-soft.lighten(60%),
    it.body,
  )
  set raw(theme: auto)
  show raw: set text(font: t.fonts.mono, size: _len(t.fonts.mono_size))
  show raw.where(block: true): it => block(
    width: 100%,
    inset: 0.8em,
    radius: 3pt,
    fill: luma(245),
    stroke: 0.5pt + rule,
    it,
  )
  show link: set text(fill: accent)
  show link: underline
  show strike: it => it
  set footnote.entry(separator: line(length: 30%, stroke: 0.5pt + rule))
  show footnote.entry: set text(size: body-size * 0.85)

  // ------------------------------------------------------------------- cover
  if not verify and t.cover.enabled {
    // A variant table ([cover.variants.<name>]) overrides the [cover] keys.
    // Precedence: the `cover:` argument (main.typ), then the `cover` input, then the theme default.
    let variants = t.cover.at("variants", default: (:))
    let variant = if cover != none { cover } else { sys.inputs.at("cover", default: t.cover.at("variant", default: "")) }
    let cv = if variant == "" { t.cover } else {
      if variant not in variants {
        panic("unknown cover variant \"" + variant + "\"; the theme defines: " + variants.keys().join(", "))
      }
      t.cover + variants.at(variant)
    }
    let cover-number = if number == none { cv.at("number", default: "") } else { number }
    // The department name doubles as the label above the title (one variable).
    let cover-label = dept-name
    page(
      header: none,
      footer: if cv.at("footer", default: false) and t.page.page_numbers { footer-at(0pt, 0pt, footer-content) } else { none },
      margin: (left: 0pt, right: 0pt, top: 0pt, bottom: 0pt),
    )[
      #if cv.at("background", default: "") != "" {
        let bleed = eval(cv.at("background_bleed", default: "0%"))
        place(center + horizon, image(cv.background, width: 100% + bleed, height: 100% + bleed, fit: "cover"))
      } else {
        place(top + left, rect(width: 100%, height: 35%, fill: accent))
      }
      #if cv.logo != "" {
        place(top + left, dx: 2.5cm, dy: 2cm, image(cv.logo, height: 2cm))
      }
      // A cover text style read from [cover] keys <prefix>_font/_size/_weight/_line_height/_uppercase/_color.
      // `edges` clips the text box (top-edge/bottom-edge) so gaps are measured from the ink.
      #let cover-text(prefix, s, default-color: "#B3B3B3", edges: (:)) = {
        let k(name, default) = cv.at(prefix + "_" + name, default: default)
        let size = _len(k("size", "14pt"))
        // line height as an absolute value: leading = line height - font size
        let line = if prefix + "_line_height" in cv { _len(k("line_height", "")) } else { size * 1.2 }
        set par(leading: line - size)
        text(
          font: k("font", t.fonts.heading),
          size: size,
          weight: _weight(k("weight", "bold")),
          fill: _color(t, k("color", default-color)),
          hyphenate: false,
          ..edges,
          if k("uppercase", false) { upper(s) } else { s },
        )
      }
      #place(top + left, dx: _len(cv.title_left), dy: eval(cv.title_top), block(width: eval(cv.title_width))[
        #set text(font: t.fonts.heading, fill: _color(t, cv.title_color), hyphenate: false)
        #set par(justify: false, spacing: 0pt)
        // The label (= department name) shares the department-name style ([cover] dept_text_*); label_gap is ink to ink.
        #let label-block = if cover-label != "" {
          cover-text("dept_text", cover-label, default-color: cv.title_color, edges: (bottom-edge: "baseline"))
        } else { none }
        #let title-block = cover-text("title", title, default-color: "#FFFFFF", edges: (top-edge: "cap-height"))
        #stack(dir: ttb, spacing: _len(cv.at("label_gap", default: "0.3em")), ..(label-block, title-block).filter(x => x != none))
        #if cv.subtitle != "" { v(0.4em); text(size: 16pt, cv.subtitle) }
      ])
      #if dept != "" or dept-name != "" or doc-version != "" {
        place(bottom + left,
          dx: _len(cv.at("dept_left", default: t.page.margin_left)),
          dy: -_len(cv.at("dept_bottom", default: t.page.margin_bottom)), block[
          #set text(font: t.fonts.heading, fill: _color(t, cv.at("dept_color", default: "#B3B3B3")), weight: "bold")
          #set par(leading: 0.4em, spacing: 0pt)
          #let symbol = if dept != "" {
            text(
              font: cv.at("dept_symbol_font", default: t.fonts.heading),
              weight: _weight(cv.at("dept_symbol_weight", default: "bold")),
              stretch: eval(cv.at("dept_symbol_stretch", default: "100%")),
              size: _len(cv.at("dept_symbol_size", default: "50pt")),
              fill: _color(t, cv.at("dept_symbol_color", default: cv.at("dept_color", default: "#B3B3B3"))),
              // box the symbol from baseline to cap height so dept_symbol_gap is measured from its ink
              top-edge: "cap-height", bottom-edge: "baseline",
              dept,
            )
          } else { none }
          #let dept-text(s) = cover-text("dept_text", s, edges: (top-edge: "cap-height"))
          #let names = grid(
            columns: (_len(cv.at("dept_name_width", default: "3.15cm")), auto),
            column-gutter: _len(cv.at("dept_gap", default: "1cm")),
            align: (left + bottom, left + bottom),
            dept-text(dept-name),
            dept-text(doc-version),
          )
          // A stack keeps dept_symbol_gap exact: no paragraph spacing sneaks in between.
          #stack(dir: ttb, spacing: _len(cv.at("dept_symbol_gap", default: "0.3em")), ..(symbol, names).filter(x => x != none))
        ])
      }
      #if cover-number != "" {
        // Compensate the last glyph's right side bearing so the ink touches the edge.
        let bearings = cv.at("number_right_bearing", default: (:))
        let rsb = bearings.at(cover-number.last(), default: 0) * _len(cv.number_size)
        place(
          bottom + right,
          dx: -_len(cv.number_inset_right) + rsb,
          dy: -_len(cv.number_inset_bottom),
          // A tight box is the gradient's reference frame: text gradients must be
          // relative to the parent. The angle follows Typst's convention (0deg
          // left-to-right, 90deg top-to-bottom), so 225deg runs bottom-right to top-left.
          box(text(
            font: cv.number_font,
            weight: _weight(cv.number_weight),
            stretch: eval(cv.number_stretch),
            size: _len(cv.number_size),
            fill: gradient.linear(
              angle: eval(cv.at("number_gradient_angle", default: "225deg")),
              _color(t, cv.number_color_start), _color(t, cv.number_color_end),
              relative: "parent",
            ),
            cover-number,
          )),
        )
      }
    ]
  }

  // --------------------------------------------------------------------- toc
  if not verify and t.toc.enabled {
    show outline.entry.where(level: 1): it => {
      set text(weight: "bold", font: t.fonts.heading, fill: _color(t, t.toc.at("section_color", default: t.text.h1.color)))
      set block(above: 1.1em)
      context {
        // "Section 01." prefix for numbered chapters (front matter has no number)
        let el = it.element
        let prefix = if el.numbering == none { none } else {
          let n = counter(heading).at(el.location()).first()
          [#t.headings.chapter_label #numbering(t.headings.chapter_number_format, n).]
        }
        let leader = box(width: 1fr, repeat(t.toc.at("leader_section", default: ".")))
        it.indented(prefix, [#it.body() #leader #it.page()])
      }
    }
    show outline.entry.where(level: 2): set text(fill: _color(t, t.toc.at("level2_color", default: "#000000")))
    show outline.entry: it => {
      if it.level >= 2 {
        // non-section entries: their own leader between the text and the page number
        set text(fill: _color(t, t.toc.at("deeper_color", default: "#404040"))) if it.level >= 3
        let leader = box(width: 1fr, repeat(t.toc.at("leader_other", default: "-")))
        it.indented(it.prefix(), [#it.body() #leader #it.page()])
      } else { it }
    }
    pagebreak(weak: true)
    block(below: 1.5em, text(
      font: t.toc.at("title_font", default: t.fonts.heading),
      size: _len(t.toc.at("title_size", default: "24pt")),
      weight: "bold",
      fill: _color(t, t.toc.at("title_color", default: t.text.h1.color)),
      t.toc.title,
    ))
    outline(title: none, depth: t.toc.depth, indent: 1.2em)
    pagebreak(weak: true)
  }

  body
}
