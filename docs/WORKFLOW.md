# Working in Livery

The window has three panes: **documents, treatments and design history** on the left, the
**preview** in the middle, the **assistant** on the right.

## 1. Project and import

Create a project (top left) and **Import…** `.docx` files. Each file is copied into the project
(the original is never touched), converted, rendered with the neutral design and checked. A
document shows **PASS**, **WARN** (rendered, with something to look at) or **FAIL** (blocked from
export). **Verification** under the preview lists every check.

**Replace** loads a new version of a document; earlier versions are kept.

## 2. Select

In the preview, hovering shows what can be selected: headings, body paragraphs and whole
tables. Click to select; Ctrl- or Shift-click to add more. A table or paragraph that runs over
several pages is one element with one outline per page. Lists, figures and other blocks are
outlined in grey and cannot be attached yet.

Selections appear above the message box as references like **Table 8 · Page 4**. Only the
reference goes to the assistant, never the document's text.

## 3. Ask

Describe the change: *"Dark blue header with white text, no vertical lines, and make the first
column twice as wide."* The assistant edits the design (theme, reusable treatments, overrides
for single elements, Typst show rules), renders lorem-ipsum copies of your documents to check
its work, and reports back.

What you type is sent to the assistant's provider (Anthropic for Claude Code, OpenAI for Codex),
as the note under the message box says. Changes to a single element stay on that element
unless you ask for something broader ("all tables like this", "every heading").

To show a look instead of describing it, attach a reference image: the picture button in the
message box, or paste or drop an image there (PNG, JPEG, WebP or GIF, up to four per message).
*"Make all tables look like this"* with a screenshot of a template table works well. The
assistant matches fills, colours, rules, weights and spacing with the fonts Livery has, and says
what it could not match. Images from earlier messages stay available in the same conversation
("like the image from before"); a new conversation starts without them.

An image is sent to the provider **as it is**: unlike your documents, it is not replaced by
lorem ipsum. Livery only removes its metadata. Use a template, a style guide or a page that
holds nothing confidential.

**Cancel** stops a running request; the proposal so far is kept. Switching between Claude Code
and Codex starts a separate conversation from the accepted design.

## 4. Review

When the assistant finishes, a **proposal** bar appears above the preview:

- **Synthetic** shows what the assistant saw: the lorem-ipsum copy with the new design.
- **Render with real content** renders the affected documents with your real text, locally.
- **Proposed** and **Compare** (accepted next to proposed) show the result. Real content can
  paginate differently from the synthetic copy; the selection outlines always come from the
  render you are looking at.
- **Accept** becomes available when every affected document rendered and passed verification.
  **Discard** drops the proposal.

If someone accepted another change in the meantime, the proposal is marked stale and cannot be
accepted: ask again from the current design. **Undo** (left, under Design) returns to the
previously accepted design. Source documents are never modified by any of this.

## 5. Reuse across the batch

Ask the assistant to make a look reusable ("make this a treatment called *Grouped figures*"),
then accept it. To apply it elsewhere:

1. Select a table that has the look's target structure and press **Find matching tables…**.
2. The list shows every table with the same structure (columns, header rows, merged header
   cells) across the project; similar tables are listed unchecked. Structure is only a hint:
   untick tables that serve a different purpose.
3. **Propose** creates a proposal for exactly the checked tables. Render, review, accept.

To give a treatment to any selected element by hand, use **Assign to selection…** under
Treatments.

## 6. Export

**Export PDFs…** renders every document with the accepted design, checks each one, and writes
the PDFs that pass to the folder you choose, with `livery-report.md` and `.json` listing
every document's status, warnings and failures. A failing document never stops the others.
