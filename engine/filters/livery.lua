--[[
livery.lua — pandoc Lua filter applied to the docx AST before the Typst writer.

Before the rules below, the project's mapping rules run (LIVERY_RULES, see the
"First pass" section and docs/MAPPING.md). They may drop blocks, which is logged and
accounted for like rule 4; they never change text.

Rules (presentation only, never touches text content):
  1. Divs/Spans created by the `+styles` docx extension are unwrapped, except
     paragraph styles listed in the style map, which become headings.
  2. Heading auto-identifiers are removed so the writer emits bare `= Title`.
  3. Image width/height in absolute units are replaced by a width relative to
     the text width so the template controls the final size. Height is dropped
     to preserve the aspect ratio.

  4. Paragraphs in Word's table-of-contents styles ("toc 1"… and the styles in
     LIVERY_DROP_STYLES) are dropped: the template regenerates the TOC. Every
     dropped paragraph is appended to LIVERY_DROP_LOG so the report can list it.
  5. A list item that consists only of a heading (Word numbers heading styles
     as list items, e.g. "A. Acronyms") is hoisted out of the list: the heading
     becomes a top-level heading and the list is split around it. The list
     number is Word presentation, not text.
  6. Headings inside table cells (Word heading styles used for header cells)
     become bold paragraphs: a heading cannot open a chapter page from inside a
     table and must not pollute the outline. Text is untouched.
  7. Internal links (targets starting with "#") are replaced by their text: the
     heading labels they point to are removed in rule 2 and Word TOC/bookmark
     links would otherwise break compilation. External links are kept.

Configuration is passed through environment variables by the Python driver:
  LIVERY_STYLE_MAP      JSON object {"Word style name": level, ...}
  LIVERY_TEXT_WIDTH_IN  text width of the source document in inches (default 6.5)
  LIVERY_DROP_STYLES    JSON array of paragraph style names to drop
  LIVERY_DROP_HEADINGS  JSON array of heading texts to drop (Word TOC titles)
  LIVERY_DROP_LOG       file that receives one line per dropped paragraph
  LIVERY_TABLE_COLUMNS  "auto" (default) drops Word column widths, "word" keeps them
  LIVERY_BLOCK_QUOTES   "unwrap" (default) turns block quotes back into paragraphs, "keep" keeps them
  LIVERY_HEADING_NUMBERS "parse" (default) derives levels from typed numbers, "keep" does not
  LIVERY_CHAPTER_WORDS  JSON array of words that introduce an explicit chapter ("Section")
  LIVERY_HEADING_LOG    file that receives one line per heading (tsv: kind, typed, level, expected, typed prefix, text)
  LIVERY_INSTRUMENT     file that receives the element inventory; enables the block markers (desktop)
  LIVERY_RULES          JSON array of the mapping's block rules {name, match, action} (first pass)
  LIVERY_RULE_LOG       file that receives {rule name: blocks matched} as JSON
]]

local json = pandoc.json

local function load_style_map()
  local raw = os.getenv("LIVERY_STYLE_MAP")
  local map = {}
  if raw and raw ~= "" then
    local ok, decoded = pcall(json.decode, raw)
    if ok and type(decoded) == "table" then
      map = decoded
    else
      io.stderr:write("livery.lua: could not parse LIVERY_STYLE_MAP\n")
    end
  end
  -- Word's built-in heading styles are already Headers in pandoc's AST, but
  -- localized / renamed variants may reach us as custom styles.
  for i = 1, 6 do
    map["Heading " .. i] = map["Heading " .. i] or i
  end
  return map
end

local STYLE_MAP = load_style_map()
local TEXT_WIDTH_IN = tonumber(os.getenv("LIVERY_TEXT_WIDTH_IN") or "") or 6.5

-- Case-insensitive lookup so "chapter title" and "Chapter Title" both match.
local STYLE_MAP_LOWER = {}
for k, v in pairs(STYLE_MAP) do
  STYLE_MAP_LOWER[string.lower(k)] = tonumber(v)
end

local DROP_STYLES = {}
do
  local raw = os.getenv("LIVERY_DROP_STYLES")
  if raw and raw ~= "" then
    local ok, decoded = pcall(json.decode, raw)
    if ok and type(decoded) == "table" then
      for _, name in ipairs(decoded) do DROP_STYLES[string.lower(name)] = true end
    end
  end
end
local DROP_HEADINGS = {}
do
  local raw = os.getenv("LIVERY_DROP_HEADINGS")
  if raw and raw ~= "" then
    local ok, decoded = pcall(json.decode, raw)
    if ok and type(decoded) == "table" then
      for _, name in ipairs(decoded) do DROP_HEADINGS[string.lower(name)] = true end
    end
  end
end
local DROP_LOG = os.getenv("LIVERY_DROP_LOG")

local function is_dropped_style(style)
  if not style then return false end
  local l = string.lower(style)
  return DROP_STYLES[l] == true or string.match(l, "^toc %d$") ~= nil
end

local function log_drop(style, el)
  if not DROP_LOG then return end
  local f = io.open(DROP_LOG, "a")
  if f then
    f:write(style, "\t", pandoc.utils.stringify(el), "\n")
    f:close()
  end
end

local function heading_level_for(style)
  if not style then return nil end
  return STYLE_MAP_LOWER[string.lower(style)]
end

-- Flatten the inlines of one or more paragraphs into a single inline list.
local function inlines_of(blocks)
  local out = pandoc.Inlines({})
  for i, b in ipairs(blocks) do
    if b.t == "Para" or b.t == "Plain" then
      if i > 1 then out:insert(pandoc.Space()) end
      out:extend(b.content)
    else
      -- Non-paragraph content inside a styled div cannot become a heading.
      return nil
    end
  end
  return out
end

local function drop_heading_text(el)
  local text = string.lower(pandoc.utils.stringify(el))
  text = text:gsub("^%s+", ""):gsub("%s+$", "")
  return DROP_HEADINGS[text] == true
end

local CAPTION_STYLES = { caption = true, ["légende"] = true, legende = true, beschriftung = true }

function Div(el)
  local style = el.attributes["custom-style"]
  if style == nil then
    return nil
  end
  if CAPTION_STYLES[string.lower(style)] then
    -- kept as a marked wrapper; the Blocks pass attaches it to the preceding image
    return pandoc.Div(el.content, pandoc.Attr("", { "livery-caption" }))
  end
  if is_dropped_style(style) then
    log_drop(style, el)
    return {}
  end
  local level = heading_level_for(style)
  if level then
    local inl = inlines_of(el.content)
    if inl and #inl > 0 then
      local h = pandoc.Header(level, inl)
      if drop_heading_text(h) then
        log_drop(style, el)
        return {}
      end
      return h
    end
  end
  return el.content -- unwrap: keep the blocks, drop the styling wrapper
end

function Span(el)
  if el.attributes["custom-style"] ~= nil then
    return el.content
  end
  -- Word bookmarks arrive as empty anchor spans; internal links are unwrapped
  -- (rule 6) so the labels they would produce are useless noise.
  if el.classes:includes("anchor") and #el.content == 0 then
    return {}
  end
  return nil
end

-- Rule 11: pandoc makes a BlockQuote out of any indented Word paragraph that is
-- not a list item. That indent is presentation, so the quote wrapper is removed.
local BLOCK_QUOTES = os.getenv("LIVERY_BLOCK_QUOTES") or "unwrap"
function BlockQuote(el)
  if BLOCK_QUOTES == "keep" then return nil end
  return el.content
end

function Link(el)
  if string.sub(el.target, 1, 1) == "#" then
    return el.content
  end
  return nil
end

function Header(el)
  local text = pandoc.utils.stringify(el):gsub("^%s+", ""):gsub("%s+$", "")
  if text == "" then
    log_drop("empty heading " .. el.level, el)
    return {}
  end
  if el.classes:includes("livery-keep") then
    -- a mapping rule kept this heading as it is: no drop by text
    el.identifier = ""
    el.classes = {}
    return el
  end
  if drop_heading_text(el) then
    log_drop("heading " .. el.level, el)
    return {}
  end
  el.identifier = ""
  el.classes = {}
  return el
end

local UNIT_TO_IN = { ["in"] = 1, ["cm"] = 1 / 2.54, ["mm"] = 1 / 25.4, ["pt"] = 1 / 72, ["px"] = 1 / 96 }

local function to_inches(dim)
  if not dim then return nil end
  local num, unit = string.match(dim, "^([%d%.]+)%s*(%a*)$")
  if not num then return nil end
  local factor = UNIT_TO_IN[unit ~= "" and unit or "px"]
  if not factor then return nil end
  return tonumber(num) * factor
end

function Image(el)
  local w_in = to_inches(el.attributes["width"])
  el.attributes["height"] = nil
  if w_in then
    local frac = math.min(w_in / TEXT_WIDTH_IN, 1.0)
    el.attributes["width"] = string.format("%.1f%%", frac * 100)
  else
    el.attributes["width"] = nil
  end
  return el
end

-- Hoist headings out of list items (rule 5). Runs after Div/Header rules
-- because pandoc applies filter functions bottom-up.
local function hoist_headings(el)
  local items = el.t == "BulletList" and el.content or el.content
  local has_heading, has_empty = false, false
  for _, item in ipairs(items) do
    if #item == 1 and item[1].t == "Header" then has_heading = true end
    if #item == 0 then has_empty = true end  -- item emptied by a drop rule
  end
  if not has_heading and not has_empty then return nil end

  local out = pandoc.Blocks({})
  local pending = {}
  local function flush()
    if #pending > 0 then
      if el.t == "BulletList" then
        out:insert(pandoc.BulletList(pending))
      else
        out:insert(pandoc.OrderedList(pending, el.listAttributes))
      end
      pending = {}
    end
  end
  for _, item in ipairs(items) do
    if #item == 1 and item[1].t == "Header" then
      flush()
      out:insert(item[1])
    elseif #item == 0 then
      -- nothing left in this item (its paragraph was dropped): skip it
    else
      table.insert(pending, item)
    end
  end
  flush()
  return out
end

BulletList = hoist_headings
OrderedList = hoist_headings

local TABLE_COLUMNS = os.getenv("LIVERY_TABLE_COLUMNS") or "auto"
local TABLE_ALIGN = os.getenv("LIVERY_TABLE_ALIGN") or "left"

-- Rule 9: long identifiers ("Lorem_ipsum_DOLOR3_sit", "a/b/c") never wrap in
-- Typst and overflow their cell. A zero-width space (U+200B) after "_" and "/"
-- gives a break opportunity; it is invisible and ignored by every content check.
local ZWSP = "\u{200B}"
local function breakable(str)
  if utf8.len(str) < 15 or not (str:find("_") or str:find("/")) then return nil end
  local out = str:gsub("([_/])", "%1" .. ZWSP)
  return out
end

-- Rule 12: a row made of one full-width cell that holds a heading or a nested
-- table is a section break disguised as a table row (Word authors use the outer
-- table as a container). The table is cut there: rows before, the cell's blocks
-- at top level (heading, nested table, text), then the remaining rows as a new
-- table. livery/table_fills.py applies the same cut so colours stay aligned.
local function has_heading_or_table(blocks)
  local found = false
  pandoc.Blocks(blocks):walk({
    Header = function() found = true end,
    Table = function() found = true end,
  })
  return found
end

local function split_container_rows(el)
  local body = el.bodies[1]
  if body == nil then return nil end
  local parts, current, hoisted_any = {}, {}, false
  for _, row in ipairs(body.body) do
    -- the one cell with content; every other cell in the row must be empty
    local content_cell, others_empty = nil, true
    for _, c in ipairs(row.cells) do
      if pandoc.utils.stringify(c.contents) ~= "" or has_heading_or_table(c.contents) then
        if content_cell then others_empty = false else content_cell = c end
      end
    end
    if content_cell and others_empty and has_heading_or_table(content_cell.contents) then
      hoisted_any = true
      table.insert(parts, { rows = current })
      table.insert(parts, { blocks = content_cell.contents })
      current = {}
    else
      table.insert(current, row)
    end
  end
  if not hoisted_any then return nil end
  table.insert(parts, { rows = current })
  local out = pandoc.Blocks({})
  local first = true
  for _, part in ipairs(parts) do
    if part.rows then
      if #part.rows > 0 or (first and #el.head.rows > 0) then
        local t = el:clone()
        t.bodies[1].body = part.rows
        if not first then t.head = pandoc.TableHead({}) end
        out:insert(t)
      end
      first = false
    else
      for _, b in ipairs(part.blocks) do out:insert(b) end
    end
  end
  return out
end

-- Rule 6: no headings inside table cells. Rules 8/9/10: column widths, long
-- words, alignment. Applied to every table, including the parts of a split.
local function style_table(el)
  local changed = false
  el = el:walk({
    Str = function(str)
      local t = breakable(str.text)
      if t then
        changed = true
        return pandoc.Str(t)
      end
      return nil
    end,
  })
  if TABLE_COLUMNS ~= "word" then
    -- Word's widths are often too narrow for the words they hold and Typst never
    -- breaks inside a word; auto-sized columns give every word its space.
    local specs = {}
    for i, spec in ipairs(el.colspecs) do specs[i] = { spec[1], nil } end
    el.colspecs = specs
    changed = true
  end
  if TABLE_ALIGN ~= "word" then
    -- Rule 10: one alignment for every column (an explicit value: pandoc's
    -- `align(center)` wrapper would otherwise be inherited through `auto`).
    local al = ({ left = pandoc.AlignLeft, center = pandoc.AlignCenter, right = pandoc.AlignRight })[TABLE_ALIGN] or pandoc.AlignLeft
    local specs = {}
    for i, spec in ipairs(el.colspecs) do specs[i] = { al, spec[2] } end
    el.colspecs = specs
    el = el:walk({ Cell = function(c) c.alignment = pandoc.AlignDefault; return c end })
    changed = true
  end
  el = el:walk({
    Header = function(h)
      changed = true
      return pandoc.Plain({ pandoc.Strong(h.content) })
    end,
  })
  if changed then return el end
  return nil
end

function Table(el)
  local split = split_container_rows(el)
  if split then
    local out = pandoc.Blocks({})
    for _, b in ipairs(split) do
      if b.t == "Table" then out:insert(style_table(b) or b) else out:insert(b) end
    end
    return out
  end
  return style_table(el)
end

-- ===========================================================================
-- Second pass (runs after everything above): heading structure from typed
-- numbers. See [source] heading_numbers in theme.toml.
-- ===========================================================================
local HEADING_NUMBERS = os.getenv("LIVERY_HEADING_NUMBERS") or "parse"
local HEADING_LOG = os.getenv("LIVERY_HEADING_LOG")
local CHAPTER_WORDS = {}
do
  local raw = os.getenv("LIVERY_CHAPTER_WORDS")
  local ok, decoded = false, nil
  if raw and raw ~= "" then ok, decoded = pcall(json.decode, raw) end
  if ok and type(decoded) == "table" then
    for _, w in ipairs(decoded) do CHAPTER_WORDS[string.lower(w)] = true end
  else
    for _, w in ipairs({ "section", "chapter", "chapitre", "part", "partie" }) do CHAPTER_WORDS[w] = true end
  end
end

local function log_heading(kind, typed, level, expected, text, prefix)
  if not HEADING_LOG then return end
  local f = io.open(HEADING_LOG, "a")
  if f then
    f:write(kind, "\t", typed or "", "\t", tostring(level), "\t", expected or "", "\t", prefix or "", "\t", text, "\n")
    f:close()
  end
end

-- Parse a typed number prefix from the heading inlines.
-- Returns kind ("chapter" | "number" | nil), components (list of ints),
-- and the number of leading inlines that make up the prefix.
local function parse_prefix(inlines)
  local function str(i)
    local el = inlines[i]
    if el and el.t == "Str" then return el.text end
    return nil
  end
  local first = str(1)
  if not first then return nil end
  -- "Section 01." / "Chapter 3"  -> chapter word + number in the next Str
  if CHAPTER_WORDS[string.lower(first)] and inlines[2] and inlines[2].t == "Space" then
    local second = str(3)
    if second then
      local num = second:match("^(%d+)%.?$")
      if num and inlines[4] then
        return "chapter", { tonumber(num) }, 3
      end
    end
  end
  -- "1." / "2.1." / "3.2.1" (a trailing dot is optional) followed by text
  local body = first:match("^(%d+[%.%d]*)%.?$")
  if body and inlines[2] then
    local comps = {}
    for n in body:gmatch("%d+") do table.insert(comps, tonumber(n)) end
    if #comps > 0 then return "number", comps, 1 end
  end
  return nil
end

-- "#strong[2.1 Title]": the typed number hides inside a formatting wrapper.
-- Unwrap a leading wrapper whose first Str is a number or a chapter word so
-- parse_prefix can see it (bold/italic on a heading is presentation anyway).
local PREFIX_WRAPPERS = { Strong = true, Emph = true, Span = true, Underline = true, SmallCaps = true }
local function unwrap_prefix(inlines)
  local first = inlines[1]
  if not (first and PREFIX_WRAPPERS[first.t] and first.content and first.content[1]) then return inlines end
  local inner = first.content[1]
  if inner.t ~= "Str" then return inlines end
  local looks = inner.text:match("^%d+[%.%d]*%.?$") or CHAPTER_WORDS[string.lower(inner.text)]
  if not looks then return inlines end
  local out = pandoc.Inlines({})
  for _, x in ipairs(first.content) do out:insert(x) end
  for k = 2, #inlines do out:insert(inlines[k]) end
  return out
end

local function prefix_text(inlines, count)
  local out = pandoc.Inlines({})
  for k = 1, count do out:insert(inlines[k]) end
  return pandoc.utils.stringify(out)
end

local function strip_prefix(inlines, count)
  local out = pandoc.Inlines({})
  local i = count + 1
  while inlines[i] and inlines[i].t == "Space" do i = i + 1 end
  for k = i, #inlines do out:insert(inlines[k]) end
  return out
end

local function comps_str(comps)
  local parts = {}
  for _, c in ipairs(comps) do table.insert(parts, tostring(c)) end
  return table.concat(parts, ".")
end

local function renumber(doc)
  if HEADING_NUMBERS == "keep" then return nil end
  -- First scan: is there any typed numbering, and are chapters declared explicitly?
  local any_numbered, any_chapter_word = false, false
  doc:walk({ Header = function(h)
    local kind = parse_prefix(unwrap_prefix(h.content))
    if kind then any_numbered = true end
    if kind == "chapter" then any_chapter_word = true end
  end })
  if not any_numbered then return nil end

  local offset = any_chapter_word and 1 or 0
  local counters = {}
  local seen_numbered = false

  local function bump(level)
    counters[level] = (counters[level] or 0) + 1
    for l = level + 1, 9 do counters[l] = 0 end
    local exp = {}
    for l = 1, level do table.insert(exp, counters[l] or 0) end
    return exp
  end

  return doc:walk({ Header = function(h)
    local text = pandoc.utils.stringify(h)
    h.content = unwrap_prefix(h.content)
    local kind, comps, count = parse_prefix(h.content)
    if kind == "chapter" then
      seen_numbered = true
      h.level = 1
      local prefix = prefix_text(h.content, count)
      h.content = strip_prefix(h.content, count)
      local exp = bump(1)
      log_heading("chapter", comps_str(comps), 1, comps_str(exp), pandoc.utils.stringify(h), prefix)
      return h
    elseif kind == "number" then
      seen_numbered = true
      local level = #comps + offset
      if level > 6 then level = 6 end
      h.level = level
      local prefix = prefix_text(h.content, count)
      h.content = strip_prefix(h.content, count)
      local exp = bump(level)
      -- typed components correspond to the levels below the explicit chapter
      local exp_cmp = {}
      for l = 1 + offset, level do table.insert(exp_cmp, exp[l]) end
      log_heading("number", comps_str(comps), level, comps_str(exp_cmp), pandoc.utils.stringify(h), prefix)
      return h
    else
      if not seen_numbered then
        -- front matter before the first numbered heading: keep it out of the numbering
        h.classes = { "unnumbered" }
        log_heading("front-matter", "", h.level, "", text)
      elseif h.level == 1 then
        -- a chapter without a typed number still counts as a chapter (its
        -- children may carry "4.x" numbers) and opens a section cover
        local exp = bump(1)
        log_heading("plain", "", 1, comps_str(exp), text)
      else
        -- a heading written without a number in a document that numbers its
        -- headings by hand: keep the heading style and the contents entry, no number
        h.classes = { "unnumbered" }
        log_heading("plain", "", h.level, "", text)
      end
      return h
    end
  end })
end

-- Rule 13: an image standing alone in a paragraph becomes a figure (centred, with
-- the Word width kept), and a following "Caption"-styled paragraph becomes its
-- centred label. A caption that follows nothing becomes an ordinary paragraph.
local function lone_image(block)
  if block.t ~= "Para" and block.t ~= "Plain" then return nil end
  local img = nil
  for _, inl in ipairs(block.content) do
    if inl.t == "Image" then
      if img then return nil end
      img = inl
    elseif inl.t ~= "Space" and inl.t ~= "SoftBreak" then
      return nil
    end
  end
  return img
end

local function is_caption(block)
  return block ~= nil and block.t == "Div" and block.classes:includes("livery-caption")
end

function Blocks(blocks)
  local out = pandoc.Blocks({})
  local i = 1
  while i <= #blocks do
    local b = blocks[i]
    local img = lone_image(b)
    if img then
      local caption = nil
      if is_caption(blocks[i + 1]) then
        caption = pandoc.Caption(blocks[i + 1].content)
        i = i + 1
      end
      out:insert(pandoc.Figure({ pandoc.Plain({ img }) }, caption))
    elseif is_caption(b) then
      for _, inner in ipairs(b.content) do out:insert(inner) end
    else
      out:insert(b)
    end
    i = i + 1
  end
  return out
end

-- ===========================================================================
-- Third pass (desktop only): element instrumentation. When LIVERY_INSTRUMENT
-- names a file, every top-level block (and every block directly inside a Div)
-- gets an opaque element ID. The Typst output brackets the block with two
-- invisible markers, `#metadata("<id>") <we-s>` before and `<we-e>` after;
-- they carry no text, so the content checks see exactly what they saw before
-- (the verifier strips them before reading content.typ back). The inventory
-- of IDs, kinds and table shapes is written to LIVERY_INSTRUMENT as JSON.
-- ===========================================================================
local INSTRUMENT = os.getenv("LIVERY_INSTRUMENT")
local KIND_PREFIX = { heading = "h", paragraph = "p", table = "t", list = "l", figure = "f", other = "x" }

local function block_kind(b)
  if b.t == "Header" then return "heading" end
  if b.t == "Para" or b.t == "Plain" then return "paragraph" end
  if b.t == "Table" then return "table" end
  if b.t == "BulletList" or b.t == "OrderedList" or b.t == "DefinitionList" then return "list" end
  if b.t == "Figure" then return "figure" end
  return "other"
end

local function cell_spans(rows)
  local out = {}
  for r, row in ipairs(rows) do
    local col = 1
    for _, cell in ipairs(row.cells) do
      if cell.row_span > 1 or cell.col_span > 1 then
        out[#out + 1] = { row = r, col = col, rowspan = cell.row_span, colspan = cell.col_span }
      end
      col = col + cell.col_span
    end
  end
  return out
end

local function table_shape(t)
  local body_rows, body_spans = 0, 0
  for _, body in ipairs(t.bodies) do
    body_rows = body_rows + #body.body
    body_spans = body_spans + #cell_spans(body.body)
  end
  return {
    columns = #t.colspecs,
    header_rows = #t.head.rows,
    header_spans = cell_spans(t.head.rows),
    body_rows = body_rows,
    body_spans = body_spans,
    widths = (function()
      local w = {}
      for i, spec in ipairs(t.colspecs) do w[i] = (type(spec[2]) == "number") and spec[2] or 0 end
      return w
    end)(),
  }
end

local function instrument(doc)
  if not INSTRUMENT or INSTRUMENT == "" then return nil end
  local inventory, n = {}, 0
  local function marker(id, label)
    return pandoc.RawBlock("typst", '#metadata("' .. id .. '") <' .. label .. ">")
  end
  local function walk(blocks)
    local out = pandoc.Blocks({})
    for _, b in ipairs(blocks) do
      if b.t == "Div" then
        b.content = walk(b.content)
        out:insert(b)
      elseif b.t == "RawBlock" or b.t == "HorizontalRule" then
        out:insert(b)
      else
        n = n + 1
        local kind = block_kind(b)
        local id = KIND_PREFIX[kind] .. string.format("%04d", n)
        local entry = { id = id, kind = kind, text = pandoc.utils.stringify(b):sub(1, 160) }
        if kind == "heading" then entry.level = b.level end
        if kind == "table" then entry.table = table_shape(b) end
        inventory[#inventory + 1] = entry
        out:insert(marker(id, "we-s"))
        out:insert(b)
        out:insert(marker(id, "we-e"))
      end
    end
    return out
  end
  doc.blocks = walk(doc.blocks)
  local f = io.open(INSTRUMENT, "w")
  f:write(json.encode(inventory))
  f:close()
  return doc
end

-- ===========================================================================
-- First pass: the project's mapping rules (docs/MAPPING.md). LIVERY_RULES is
-- a JSON array of {name, match, action}, already validated by the Python
-- driver; page rules are applied to the Word file before pandoc and never get
-- here. Rules are tried in order and the first match decides; a block no rule
-- matches is left to the built-in rules above. Every drop is logged to
-- LIVERY_DROP_LOG as "rule:<name>", and the match count of every rule is
-- written to LIVERY_RULE_LOG as JSON {name: count}.
-- ===========================================================================
local RULES = {}
do
  local raw = os.getenv("LIVERY_RULES")
  if raw and raw ~= "" then
    local ok, decoded = pcall(json.decode, raw)
    if ok and type(decoded) == "table" then
      RULES = decoded
    else
      io.stderr:write("livery.lua: could not parse LIVERY_RULES\n")
    end
  end
end
local RULE_LOG = os.getenv("LIVERY_RULE_LOG")

local function as_list(v)
  if type(v) == "table" then return v end
  return { v }
end

local function trimmed_lower(s)
  return (string.lower(s):gsub("^%s+", ""):gsub("%s+$", ""))
end

-- Every word of the inlines is inside Strong (and there is at least one).
local function all_bold(inlines)
  local strong, plain = false, false
  local function scan(list, inside)
    for _, x in ipairs(list) do
      if x.t == "Str" then
        if inside then strong = true else plain = true end
      elseif x.t == "Strong" then
        scan(x.content, true)
      elseif x.content and type(x.content) ~= "string" then
        scan(x.content, inside)
      end
    end
  end
  scan(inlines, false)
  return strong and not plain
end

local function block_inlines(b)
  if b.t == "Header" or b.t == "Para" or b.t == "Plain" then return b.content end
  return nil
end

local function contains_table(t)
  local found = false
  local function cells(rows)
    for _, row in ipairs(rows) do
      for _, c in ipairs(row.cells) do
        pandoc.Blocks(c.contents):walk({ Table = function() found = true end })
      end
    end
  end
  cells(t.head.rows)
  for _, body in ipairs(t.bodies) do cells(body.head); cells(body.body) end
  cells(t.foot.rows)
  return found
end

-- What the rules see of a block: kind, Word style, text and shape.
local function describe(b, style, ctx)
  local inner = b
  if b.t == "Div" and #b.content == 1 then inner = b.content[1] end
  local kind = block_kind(inner)
  if b.t == "Div" and heading_level_for(style) and #b.content >= 1 then kind = "heading" end
  local d = { kind = kind, style = style, in_table = ctx.in_table, before_first_heading = not ctx.seen_heading }
  if inner.t == "Header" then
    d.level = inner.level
    d.style = style or ("Heading " .. inner.level)
  elseif kind == "heading" then
    d.level = heading_level_for(style)
  elseif inner.t == "Para" or inner.t == "Plain" then
    d.style = style or "Normal"
  end
  local inl = block_inlines(inner)
  if b.t == "Div" and not inl then inl = inlines_of(b.content) end
  d.inlines = inl
  d.text = pandoc.utils.stringify(b)
  if inner.t == "Table" then d.contains_table = contains_table(inner) end
  return d
end

local function matches(m, d)
  if m.kind and m.kind ~= d.kind then return false end
  if m.style then
    if not d.style then return false end
    local ok = false
    for _, s in ipairs(as_list(m.style)) do
      if string.lower(s) == string.lower(d.style) then ok = true end
    end
    if not ok then return false end
  end
  if m.level and m.level ~= d.level then return false end
  if m.in_table ~= nil and m.in_table ~= d.in_table then return false end
  if m.before_first_heading ~= nil and m.before_first_heading ~= d.before_first_heading then return false end
  if m.contains_table ~= nil and m.contains_table ~= (d.contains_table == true) then return false end
  if m.bold ~= nil then
    if not d.inlines or all_bold(d.inlines) ~= m.bold then return false end
  end
  if m.numbered ~= nil then
    local numbered = d.inlines ~= nil and parse_prefix(unwrap_prefix(d.inlines)) ~= nil
    if numbered ~= m.numbered then return false end
  end
  if m.upper ~= nil then
    local letters = d.text:gsub("[^%a]", "")
    local upper = letters ~= "" and letters == string.upper(letters)
    if upper ~= m.upper then return false end
  end
  if m.text then
    local t, ok = trimmed_lower(d.text), false
    for _, s in ipairs(as_list(m.text)) do if trimmed_lower(s) == t then ok = true end end
    if not ok then return false end
  end
  if m.text_starts then
    local t, ok = trimmed_lower(d.text), false
    for _, s in ipairs(as_list(m.text_starts)) do
      local p = trimmed_lower(s)
      if t:sub(1, #p) == p then ok = true end
    end
    if not ok then return false end
  end
  return true
end

local function cell_blocks(t)
  local out = pandoc.Blocks({})
  local function rows(list)
    for _, row in ipairs(list) do
      for _, c in ipairs(row.cells) do out:extend(c.contents) end
    end
  end
  rows(t.head.rows)
  for _, body in ipairs(t.bodies) do rows(body.head); rows(body.body) end
  rows(t.foot.rows)
  return out
end

local function each_cell(t, fn)
  local function rows(list)
    for _, row in ipairs(list) do
      for _, c in ipairs(row.cells) do c.contents = fn(c.contents) end
    end
  end
  rows(t.head.rows)
  for _, body in ipairs(t.bodies) do rows(body.head); rows(body.body) end
  rows(t.foot.rows)
end

local function unwrap_inner_tables(blocks)
  local out = pandoc.Blocks({})
  for _, b in ipairs(blocks) do
    if b.t == "Table" then out:extend(unwrap_inner_tables(cell_blocks(b))) else out:insert(b) end
  end
  return out
end

-- Apply one rule's action. Returns the replacement blocks.
local function act(action, b, d, name)
  if action == "drop" then
    log_drop("rule:" .. name, b)
    return pandoc.Blocks({})
  end
  local inner = (b.t == "Div" and #b.content == 1) and b.content[1] or b
  if action == "keep" then
    if inner.t == "Header" then inner.classes:insert("livery-keep") end
    if b.t == "Div" then return pandoc.Blocks(b.content) end
    return pandoc.Blocks({ b })
  end
  if type(action) ~= "table" then return pandoc.Blocks({ b }) end
  if action.heading ~= nil and d.inlines and #d.inlines > 0 then
    local level = action.heading
    if level == "from_number" then
      local kind, comps = parse_prefix(unwrap_prefix(d.inlines))
      if kind == "chapter" then level = 1
      elseif kind == "number" then level = math.min(#comps, 6)
      else return pandoc.Blocks({ b }) end
    end
    return pandoc.Blocks({ pandoc.Header(level, d.inlines) })
  end
  if action.paragraph and d.inlines then
    return pandoc.Blocks({ pandoc.Para(d.inlines) })
  end
  if action.nested_table and inner.t == "Table" then
    if action.nested_table == "unwrap_outer" then return cell_blocks(inner) end
    if action.nested_table == "unwrap_inner" then
      each_cell(inner, unwrap_inner_tables)
      return pandoc.Blocks({ inner })
    end
  end
  return pandoc.Blocks({ b })
end

local function apply_rules(doc)
  if #RULES == 0 then return nil end
  local counts = {}
  for _, r in ipairs(RULES) do counts[r.name] = 0 end
  local walk
  local function visit(b, ctx)
    local style = nil
    if b.t == "Div" then style = b.attributes["custom-style"] end
    local candidate = style ~= nil or b.t == "Header" or b.t == "Para" or b.t == "Plain" or b.t == "Table"
      or b.t == "BulletList" or b.t == "OrderedList" or b.t == "Figure"
    if candidate then
      local d = describe(b, style, ctx)
      for _, r in ipairs(RULES) do
        if matches(r.match or {}, d) then
          counts[r.name] = counts[r.name] + 1
          local out = act(r.action, b, d, r.name)
          if d.kind == "heading" and not ctx.in_table then ctx.seen_heading = true end
          local nested = type(r.action) == "table" and r.action.nested_table
          if nested == "unwrap_outer" then
            -- the cells' blocks are new to the rules; every unwrap removes a table, so this ends
            return walk(out, ctx)
          elseif nested == "unwrap_inner" and #out == 1 and out[1].t == "Table" then
            local inner_ctx = { in_table = true, seen_heading = ctx.seen_heading }
            each_cell(out[1], function(blocks) return walk(blocks, inner_ctx) end)
          end
          return out
        end
      end
      if d.kind == "heading" and not ctx.in_table then ctx.seen_heading = true end
    end
    if b.t == "Table" then
      local inner_ctx = { in_table = true, seen_heading = ctx.seen_heading }
      each_cell(b, function(blocks) return walk(blocks, inner_ctx) end)
    elseif b.t == "BulletList" or b.t == "OrderedList" then
      local items = {}
      for i, item in ipairs(b.content) do items[i] = walk(item, ctx) end
      b.content = items
    elseif (b.t == "Div" and style == nil) or b.t == "BlockQuote" then
      b.content = walk(b.content, ctx)
    end
    return pandoc.Blocks({ b })
  end
  walk = function(blocks, ctx)
    local out = pandoc.Blocks({})
    for _, b in ipairs(blocks) do out:extend(visit(b, ctx)) end
    return out
  end
  doc.blocks = walk(doc.blocks, { in_table = false, seen_heading = false })
  if RULE_LOG and RULE_LOG ~= "" then
    local f = io.open(RULE_LOG, "w")
    if f then
      f:write(json.encode(counts))
      f:close()
    end
  end
  return doc
end

-- pandoc applies these filter tables in order.
return {
  { Pandoc = apply_rules },
  { Div = Div, Span = Span, Link = Link, Header = Header, Image = Image, BlockQuote = BlockQuote, Blocks = Blocks,
    BulletList = BulletList, OrderedList = OrderedList, Table = Table },
  { Pandoc = renumber },
  { Pandoc = instrument },
}

