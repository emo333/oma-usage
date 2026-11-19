//! Single-line search/research field. The platform input handler receives IME text;
//! raw key events are only used for editing commands, never to reconstruct text.
use crate::theme::Palette;
use gpui::{prelude::*, *};
use std::ops::Range;

pub struct Field {
    pub focus: FocusHandle,
    pub text: String,
    anchor: usize,
    cursor: usize,
    marked: Option<Range<usize>>,
    line: Option<(ShapedLine, Point<Pixels>)>,
    palette: Palette,
}

impl Field {
    pub fn new(text: String, palette: Palette, cx: &mut Context<Self>) -> Self {
        Self {
            focus: cx.focus_handle().tab_stop(true),
            cursor: text.len(),
            anchor: 0,
            text,
            marked: None,
            line: None,
            palette,
        }
    }
    fn selected(&self) -> Range<usize> {
        self.anchor.min(self.cursor)..self.anchor.max(self.cursor)
    }
    fn byte(text: &str, utf16: usize) -> usize {
        let mut units = 0;
        for (offset, ch) in text.char_indices() {
            if units >= utf16 {
                return offset;
            }
            units += ch.len_utf16();
        }
        text.len()
    }
    fn bytes(&self, r: Range<usize>) -> Range<usize> {
        Self::byte(&self.text, r.start)..Self::byte(&self.text, r.end)
    }
    fn units(&self, r: Range<usize>) -> Range<usize> {
        self.text[..r.start].encode_utf16().count()..self.text[..r.end].encode_utf16().count()
    }
    fn replace(&mut self, r: Option<Range<usize>>, s: &str) -> Range<usize> {
        let r = r
            .map(|r| self.bytes(r))
            .or_else(|| self.marked.take())
            .unwrap_or_else(|| self.selected());
        self.marked = None;
        if s.contains(char::is_control) || self.text.len() - r.len() + s.len() > 1024 {
            return self.selected();
        }
        self.text.replace_range(r.clone(), s);
        self.cursor = r.start + s.len();
        self.anchor = self.cursor;
        r.start..self.cursor
    }
    fn key(&mut self, e: &KeyDownEvent, _: &mut Window, cx: &mut Context<Self>) {
        let k = &e.keystroke;
        let selected = self.selected();
        let prev = self.text[..self.cursor]
            .char_indices()
            .next_back()
            .map_or(0, |(i, _)| i);
        let next = self.cursor
            + self.text[self.cursor..]
                .chars()
                .next()
                .map_or(0, char::len_utf8);
        match (k.modifiers.control, k.key.as_str()) {
            (true, "a") => {
                self.anchor = 0;
                self.cursor = self.text.len();
            }
            (true, "v") => {
                if let Some(s) = cx.read_from_clipboard().and_then(|i| i.text()) {
                    self.replace(None, &s);
                }
            }
            (true, "c" | "x") => {
                if !selected.is_empty() {
                    cx.write_to_clipboard(ClipboardItem::new_string(self.text[selected].into()));
                    if k.key == "x" {
                        self.replace(None, "");
                    }
                }
            }
            (false, "backspace" | "delete") => {
                if selected.is_empty() {
                    self.anchor = if k.key == "backspace" { prev } else { next };
                }
                self.replace(None, "");
            }
            (false, "left" | "right" | "home" | "end") => {
                self.cursor = match k.key.as_str() {
                    "home" => 0,
                    "end" => self.text.len(),
                    "left" if !k.modifiers.shift && !selected.is_empty() => selected.start,
                    "right" if !k.modifiers.shift && !selected.is_empty() => selected.end,
                    "left" => prev,
                    _ => next,
                };
                if !k.modifiers.shift {
                    self.anchor = self.cursor;
                }
            }
            _ => return,
        }
        self.marked = None;
        cx.stop_propagation();
        cx.notify();
    }
}

impl EntityInputHandler for Field {
    fn text_for_range(
        &mut self,
        range: Range<usize>,
        actual: &mut Option<Range<usize>>,
        _: &mut Window,
        _: &mut Context<Self>,
    ) -> Option<String> {
        let b = self.bytes(range);
        *actual = Some(self.units(b.clone()));
        Some(self.text[b].into())
    }
    fn selected_text_range(
        &mut self,
        _: bool,
        _: &mut Window,
        _: &mut Context<Self>,
    ) -> Option<UTF16Selection> {
        Some(UTF16Selection {
            range: self.units(self.selected()),
            reversed: self.anchor > self.cursor,
        })
    }
    fn marked_text_range(&self, _: &mut Window, _: &mut Context<Self>) -> Option<Range<usize>> {
        self.marked.clone().map(|r| self.units(r))
    }
    fn unmark_text(&mut self, _: &mut Window, _: &mut Context<Self>) {
        self.marked = None;
    }
    fn replace_text_in_range(
        &mut self,
        r: Option<Range<usize>>,
        s: &str,
        _: &mut Window,
        cx: &mut Context<Self>,
    ) {
        self.replace(r, s);
        cx.notify();
    }
    fn replace_and_mark_text_in_range(
        &mut self,
        r: Option<Range<usize>>,
        s: &str,
        sel: Option<Range<usize>>,
        _: &mut Window,
        cx: &mut Context<Self>,
    ) {
        let b = self.replace(r, s);
        if !b.is_empty() {
            self.marked = Some(b.clone());
            if let Some(sel) = sel {
                let s = &self.text[b.clone()];
                self.anchor = b.start + Self::byte(s, sel.start);
                self.cursor = b.start + Self::byte(s, sel.end);
            }
        }
        cx.notify();
    }
    fn bounds_for_range(
        &mut self,
        r: Range<usize>,
        bounds: Bounds<Pixels>,
        _: &mut Window,
        _: &mut Context<Self>,
    ) -> Option<Bounds<Pixels>> {
        let (line, origin) = self.line.as_ref()?;
        let b = self.bytes(r);
        Some(Bounds::from_corners(
            point(origin.x + line.x_for_index(b.start), bounds.top()),
            point(origin.x + line.x_for_index(b.end), bounds.bottom()),
        ))
    }
    fn character_index_for_point(
        &mut self,
        at: Point<Pixels>,
        _: &mut Window,
        _: &mut Context<Self>,
    ) -> Option<usize> {
        let (line, origin) = self.line.as_ref()?;
        let i = line
            .closest_index_for_x(at.x - origin.x)
            .min(self.text.len());
        Some(self.text[..i].encode_utf16().count())
    }
}

impl Render for Field {
    fn render(&mut self, _: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        let entity = cx.entity();
        let p = self.palette;
        div()
            .id("edit-field")
            .tab_index(0)
            .track_focus(&self.focus)
            .cursor(CursorStyle::IBeam)
            .px_3()
            .py_2()
            .w_full()
            .bg(p.bg)
            .border_1()
            .border_color(p.accent)
            .rounded_md()
            .overflow_hidden()
            .on_key_down(cx.listener(Self::key))
            .on_mouse_down(
                MouseButton::Left,
                cx.listener(|this, e: &MouseDownEvent, w, cx| {
                    this.focus.focus(w);
                    if let Some((line, origin)) = &this.line {
                        this.cursor = line
                            .closest_index_for_x(e.position.x - origin.x)
                            .min(this.text.len());
                        if !e.modifiers.shift {
                            this.anchor = this.cursor;
                        }
                        cx.notify();
                    }
                }),
            )
            .child(
                canvas(
                    |_, _, _| (),
                    move |bounds, _, w, cx| {
                        let field = entity.read(cx);
                        let style = w.text_style();
                        let size = px(16.);
                        let text: SharedString = field.text.clone().into();
                        let run = TextRun {
                            len: text.len(),
                            font: style.font(),
                            color: p.text,
                            background_color: None,
                            underline: None,
                            strikethrough: None,
                        };
                        let line = w.text_system().shape_line(text, size, &[run], None);
                        let caret = line.x_for_index(field.cursor);
                        let origin = point(
                            bounds.left() - (caret + px(2.) - bounds.size.width).max(px(0.)),
                            bounds.top(),
                        );
                        let sel = field.selected();
                        let focus = field.focus.clone();
                        let focused = focus.is_focused(w);
                        if !sel.is_empty() && focused {
                            w.paint_quad(fill(
                                Bounds::from_corners(
                                    point(origin.x + line.x_for_index(sel.start), origin.y),
                                    point(origin.x + line.x_for_index(sel.end), bounds.bottom()),
                                ),
                                p.accent.opacity(0.32),
                            ));
                        }
                        let _ = line.paint(origin, bounds.size.height, w, cx);
                        if focused && sel.is_empty() {
                            w.paint_quad(fill(
                                Bounds::new(
                                    point(origin.x + caret, origin.y),
                                    gpui::size(px(1.5), bounds.size.height),
                                ),
                                p.accent,
                            ));
                        }
                        w.handle_input(
                            &focus,
                            ElementInputHandler::new(bounds, entity.clone()),
                            cx,
                        );
                        entity.update(cx, |field, _| field.line = Some((line, origin)));
                    },
                )
                .w_full()
                .h(px(26.)),
            )
    }
}
