--[[
wordeasy.lua — pandoc Lua filter applied to the docx AST before the Typst writer.

Rules (presentation only, never touches text content):
  1. Divs/Spans created by the `+styles` docx extension are unwrapped, except
     paragraph styles listed in the style map, which become headings.
  2. Heading auto-identifiers are removed so the writer emits bare `= Title`.
  3. Image width/height in absolute units are replaced by a width relative to
     the text width so the template controls the final size. Height is dropped
     to preserve the aspect ratio.

  4. Paragraphs in Word's table-of-contents styles ("toc 1"… and the styles in
     WORDEASY_DROP_STYLES) are dropped: the template regenerates the TOC. Every
     dropped paragraph is appended to WORDEASY_DROP_LOG so the report can list it.
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
  WORDEASY_STYLE_MAP      JSON object {"Word style name": level, ...}
  WORDEASY_TEXT_WIDTH_IN  text width of the source document in inches (default 6.5)
  WORDEASY_DROP_STYLES    JSON array of paragraph style names to drop
  WORDEASY_DROP_HEADINGS  JSON array of heading texts to drop (Word TOC titles)
  WORDEASY_DROP_LOG       file that receives one line per dropped paragraph
  WORDEASY_TABLE_COLUMNS  "auto" (default) drops Word column widths, "word" keeps them
  WORDEASY_BLOCK_QUOTES   "unwrap" (default) turns block quotes back into paragraphs, "keep" keeps them
  WORDEASY_HEADING_NUMBERS "parse" (default) derives levels from typed numbers, "keep" does not
  WORDEASY_CHAPTER_WORDS  JSON array of words that introduce an explicit chapter ("Section")
  WORDEASY_HEADING_LOG    file that receives one line per heading (tsv: kind, typed, level, expected, text)
]]

local json = pandoc.json

local function load_style_map()
  local raw = os.getenv("WORDEASY_STYLE_MAP")
  local map = {}
  if raw and raw ~= "" then
    local ok, decoded = pcall(json.decode, raw)
    if ok and type(decoded) == "table" then
      map = decoded
    else
      io.stderr:write("wordeasy.lua: could not parse WORDEASY_STYLE_MAP\n")
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
local TEXT_WIDTH_IN = tonumber(os.getenv("WORDEASY_TEXT_WIDTH_IN") or "") or 6.5

-- Case-insensitive lookup so "chapter title" and "Chapter Title" both match.
local STYLE_MAP_LOWER = {}
for k, v in pairs(STYLE_MAP) do
  STYLE_MAP_LOWER[string.lower(k)] = tonumber(v)
end

local DROP_STYLES = {}
do
  local raw = os.getenv("WORDEASY_DROP_STYLES")
  if raw and raw ~= "" then
    local ok, decoded = pcall(json.decode, raw)
    if ok and type(decoded) == "table" then
      for _, name in ipairs(decoded) do DROP_STYLES[string.lower(name)] = true end
    end
  end
end
local DROP_HEADINGS = {}
do
  local raw = os.getenv("WORDEASY_DROP_HEADINGS")
  if raw and raw ~= "" then
    local ok, decoded = pcall(json.decode, raw)
    if ok and type(decoded) == "table" then
      for _, name in ipairs(decoded) do DROP_HEADINGS[string.lower(name)] = true end
    end
  end
end
local DROP_LOG = os.getenv("WORDEASY_DROP_LOG")

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
    return pandoc.Div(el.content, pandoc.Attr("", { "wordeasy-caption" }))
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
local BLOCK_QUOTES = os.getenv("WORDEASY_BLOCK_QUOTES") or "unwrap"
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

local TABLE_COLUMNS = os.getenv("WORDEASY_TABLE_COLUMNS") or "auto"
local TABLE_ALIGN = os.getenv("WORDEASY_TABLE_ALIGN") or "left"

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
-- table. wordeasy/table_fills.py applies the same cut so colours stay aligned.
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
local HEADING_NUMBERS = os.getenv("WORDEASY_HEADING_NUMBERS") or "parse"
local HEADING_LOG = os.getenv("WORDEASY_HEADING_LOG")
local CHAPTER_WORDS = {}
do
  local raw = os.getenv("WORDEASY_CHAPTER_WORDS")
  local ok, decoded = false, nil
  if raw and raw ~= "" then ok, decoded = pcall(json.decode, raw) end
  if ok and type(decoded) == "table" then
    for _, w in ipairs(decoded) do CHAPTER_WORDS[string.lower(w)] = true end
  else
    for _, w in ipairs({ "section", "chapter", "chapitre", "part", "partie" }) do CHAPTER_WORDS[w] = true end
  end
end

local function log_heading(kind, typed, level, expected, text)
  if not HEADING_LOG then return end
  local f = io.open(HEADING_LOG, "a")
  if f then
    f:write(kind, "\t", typed or "", "\t", tostring(level), "\t", expected or "", "\t", text, "\n")
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
      h.content = strip_prefix(h.content, count)
      local exp = bump(1)
      log_heading("chapter", comps_str(comps), 1, comps_str(exp), pandoc.utils.stringify(h))
      return h
    elseif kind == "number" then
      seen_numbered = true
      local level = #comps + offset
      if level > 6 then level = 6 end
      h.level = level
      h.content = strip_prefix(h.content, count)
      local exp = bump(level)
      -- typed components correspond to the levels below the explicit chapter
      local exp_cmp = {}
      for l = 1 + offset, level do table.insert(exp_cmp, exp[l]) end
      log_heading("number", comps_str(comps), level, comps_str(exp_cmp), pandoc.utils.stringify(h))
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
  return block ~= nil and block.t == "Div" and block.classes:includes("wordeasy-caption")
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

-- pandoc applies these filter tables in order.
return {
  { Div = Div, Span = Span, Link = Link, Header = Header, Image = Image, BlockQuote = BlockQuote, Blocks = Blocks,
    BulletList = BulletList, OrderedList = OrderedList, Table = Table },
  { Pandoc = renumber },
}

