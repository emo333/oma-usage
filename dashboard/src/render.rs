use super::*;
use oma_usage_dashboard::{Button, Item, Options, Section};

type Cx<'a> = Context<'a, Dashboard>;
fn column() -> Div {
    div().flex().flex_col().min_w_0()
}
fn row() -> Div {
    div().flex().flex_row().items_center().min_w_0()
}
fn line(text: impl Into<SharedString>, color: Hsla, sz: f32) -> Div {
    div()
        .flex()
        .flex_col()
        .max_w_full()
        .whitespace_normal()
        .text_color(color)
        .text_size(px(sz))
        .line_height(px(sz * 1.45))
        .child(text.into())
}
fn heading(text: impl Into<SharedString>, p: Palette, sz: f32) -> Div {
    line(text, p.text, sz).font_weight(FontWeight::SEMIBOLD)
}
fn button(id: impl Into<ElementId>, p: Palette, color: Hsla) -> Stateful<Div> {
    row()
        .id(id)
        .tab_index(0)
        .gap_2()
        .justify_center()
        .px_3()
        .py_1p5()
        .cursor_pointer()
        .rounded_md()
        .border_1()
        .border_color(p.edge)
        .text_size(px(14.))
        .text_color(color)
        .hover(move |s| s.bg(color.opacity(0.12)).border_color(color))
        .focus(move |s| s.border_color(color))
        .active(|s| s.opacity(0.75))
}
fn icon(path: &'static str, sz: f32, ink: Hsla) -> Svg {
    svg().path(path).size(px(sz)).text_color(ink)
}
fn tag(text: &'static str, ink: Hsla) -> Div {
    line(text, ink, 11.).font_weight(FontWeight::BOLD)
}

impl Dashboard {
    fn action_button(
        &self,
        id: impl Into<ElementId>,
        a: &Arc<Button>,
        fallback_tone: &str,
        cx: &mut Cx,
    ) -> Stateful<Div> {
        let action = Arc::clone(a);
        let ink = self.palette.ink(if a.tone.is_empty() {
            fallback_tone
        } else {
            &a.tone
        });
        button(id, self.palette, ink)
            .bg(ink.opacity(0.07))
            .child(a.label.clone())
            .on_click(cx.listener(move |s, _, w, cx| s.perform(&action.request, w, cx)))
    }
    fn nav(&self, wide: bool, cx: &mut Cx) -> Div {
        let p = self.palette;
        let mut list = column().gap_1().w_full();
        for (i, (_, name)) in PAGES.iter().enumerate() {
            if i == 0 || i == 4 || i == 6 {
                list = list.child(div().pt_3().pb_1().pl_3().child(tag(
                    ["WORKSPACE", "ANALYSIS", "MANAGE"][if i == 0 {
                        0
                    } else if i == 4 {
                        1
                    } else {
                        2
                    }],
                    p.secondary,
                )));
            }
            let selected = i == self.page;
            list = list.child(
                row()
                    .id(("page", i))
                    .tab_index(0)
                    .px_3()
                    .py_2()
                    .gap_3()
                    .rounded_md()
                    .border_1()
                    .border_color(if selected {
                        p.accent.opacity(0.45)
                    } else {
                        p.rail
                    })
                    .when(selected, |s| s.bg(p.accent.opacity(0.12)))
                    .hover(move |s| s.bg(p.accent.opacity(0.10)))
                    .focus(move |s| s.border_color(p.accent))
                    .cursor_pointer()
                    .child(
                        div()
                            .size(px(7.))
                            .rounded_full()
                            .border_1()
                            .border_color(if selected { p.accent } else { p.secondary })
                            .when(selected, |s| s.bg(p.accent)),
                    )
                    .child(line(*name, if selected { p.accent } else { p.text }, 15.).flex_1())
                    .on_click(cx.listener(move |s, _, w, cx| s.go(i, 0, w, cx))),
            );
        }
        if !wide {
            return list;
        }
        column()
            .w(px(188.))
            .flex_shrink_0()
            .h_full()
            .bg(p.rail)
            .border_r_1()
            .border_color(p.edge)
            .px_3()
            .py_5()
            .gap_4()
            .child(
                row()
                    .gap_2()
                    .pl_2()
                    .child(
                        div()
                            .flex()
                            .items_center()
                            .justify_center()
                            .size(px(36.))
                            .rounded_md()
                            .bg(p.accent.opacity(0.14))
                            .child(icon("usage.svg", 24., p.accent)),
                    )
                    .child(column().child(heading("Oma Usage", p, 18.)).child(line(
                        "Local dashboard",
                        p.secondary,
                        12.,
                    ))),
            )
            .child(
                div()
                    .id("nav-scroll")
                    .flex_1()
                    .min_h_0()
                    .overflow_y_scroll()
                    .child(list),
            )
            .child(
                button("keys", p, p.secondary)
                    .child("Keyboard shortcuts")
                    .on_click(cx.listener(|s, _, w, cx| s.show(Layer::Keys, w, cx))),
            )
    }
    fn options(&self, opts: &Options, n: usize, cx: &mut Cx) -> Stateful<Div> {
        let p = self.palette;
        let current = self
            .filters
            .get(&self.page)
            .and_then(|f| f.get(opts.key.as_str()))
            .and_then(Value::as_str)
            .or_else(|| (!opts.selected.is_empty()).then_some(opts.selected.as_str()))
            .or_else(|| opts.values.first().map(|v| v.value.as_str()));
        let mut choices = row().flex_wrap().gap_1p5();
        for (i, v) in opts.values.iter().enumerate() {
            let key = opts.key.clone();
            let value = v.value.clone();
            let selected = current == Some(value.as_str());
            choices = choices.child(
                button(
                    ("choice", i),
                    p,
                    if selected { p.accent } else { p.secondary },
                )
                .when(selected, |s| {
                    s.border_color(p.accent).bg(p.accent.opacity(0.12))
                })
                .child(v.label.clone())
                .on_click(cx.listener(move |s, _, w, cx| {
                    if s.busy == Busy::Acting {
                        return;
                    }
                    s.filter(&key, json!(value));
                    s.refresh(w, cx)
                })),
            );
        }
        column()
            .id(("options", n))
            .gap_1()
            .child(heading(opts.label.clone(), p, 13.))
            .child(choices)
    }
    fn item(&self, i: usize, item: &Item, compact: bool, cx: &mut Cx) -> AnyElement {
        let p = self.palette;
        let contents = column()
            .flex_1()
            .gap_1()
            .child(
                row()
                    .gap_x_3()
                    .justify_between()
                    .child(heading(item.label.clone(), p, 15.).flex_1())
                    .when(!item.value.is_empty(), |s| {
                        s.child(
                            line(
                                item.value.clone(),
                                if item.tone.is_empty() {
                                    p.text
                                } else {
                                    p.ink(&item.tone)
                                },
                                14.,
                            )
                            .w(relative(0.48))
                            .text_right(),
                        )
                    }),
            )
            .when(!item.note.is_empty(), |s| {
                s.child(line(item.note.clone(), p.secondary, 13.))
            });
        let r = row()
            .gap_2()
            .px_2()
            .when(compact, |s| s.py_1p5())
            .when(!compact, |s| s.py_2())
            .border_t_1()
            .border_color(p.edge.opacity(0.75));
        if let Some(action) = &item.action {
            let a = action.clone();
            r.id(("row", i))
                .tab_index(0)
                .cursor_pointer()
                .hover(move |s| s.bg(p.accent.opacity(0.08)))
                .focus(move |s| s.bg(p.accent.opacity(0.14)))
                .child(contents)
                .child(line("›", p.accent, 20.).w(px(12.)))
                .on_click(cx.listener(move |s, _, w, cx| s.perform(&a.request, w, cx)))
                .into_any_element()
        } else {
            r.child(contents).into_any_element()
        }
    }
    fn section(
        &self,
        i: usize,
        sec: &Section,
        expanded: bool,
        modal: bool,
        cx: &mut Cx,
    ) -> Stateful<Div> {
        let p = self.palette;
        let mut title = row()
            .flex_wrap()
            .gap_2()
            .justify_between()
            .child(heading(sec.title.clone(), p, 18.).flex_1());
        if !sec.help.is_empty() {
            title = title.child(
                button("info", p, p.accent)
                    .px_2()
                    .py_1()
                    .border_color(p.accent.opacity(0.5))
                    .child(icon("info.svg", 18., p.accent))
                    .child(if expanded { "Hide info" } else { "Info" })
                    .on_click(cx.listener(move |s, _, _, cx| {
                        let set = if modal {
                            let Some(layer) = s.layers.last_mut() else {
                                return;
                            };
                            &mut layer.expanded
                        } else {
                            &mut s.expanded
                        };
                        if !set.remove(&i) {
                            set.insert(i);
                        }
                        cx.notify();
                    })),
            );
        }
        let mut card = column()
            .id(("card", i))
            .flex_shrink_0()
            .gap_3()
            .p_4()
            .bg(p.panel)
            .rounded_lg()
            .border_1()
            .border_color(p.edge)
            .child(title);
        if expanded && !sec.help.is_empty() {
            card = card.child(
                div()
                    .px_3()
                    .py_2()
                    .border_l_2()
                    .border_color(p.accent)
                    .bg(p.accent.opacity(0.08))
                    .child(line(sec.help.clone(), p.text, 14.)),
            );
        }
        if !sec.subtitle.is_empty() {
            card = card.child(
                line(
                    sec.subtitle.clone(),
                    if sec.tone.is_empty() {
                        p.secondary
                    } else {
                        p.ink(&sec.tone)
                    },
                    14.,
                )
                .w_full(),
            );
        }
        for (n, opts) in sec.options.iter().enumerate() {
            card = card.child(self.options(opts, n, cx));
        }
        if !sec.buttons.is_empty() {
            let mut actions = row().flex_wrap().gap_2();
            for (j, a) in sec.buttons.iter().enumerate() {
                actions = actions.child(self.action_button(("action", j), a, "", cx));
            }
            card = card.child(actions);
        }
        if !sec.items.is_empty() {
            let mut items = column();
            for (j, item) in sec.items.iter().enumerate() {
                items = items.child(self.item(j, item, sec.items.len() < 6, cx));
            }
            card = card.child(items);
        }
        card
    }
    fn highlights(&self, items: &[Item], compact: bool, cx: &mut Cx) -> Div {
        let p = self.palette;
        let mut group = div().flex().gap_3().when(compact, |s| s.flex_col());
        for (i, item) in items.iter().enumerate() {
            let body = column()
                .gap_1()
                .child(line(item.label.clone(), p.secondary, 13.))
                .child(heading(item.value.clone(), p, 22.))
                .when(!item.note.is_empty(), |s| {
                    s.child(line(item.note.clone(), p.secondary, 12.))
                });
            let tile = div()
                .id(("metric", i))
                .min_w_0()
                .flex_1()
                .p_3()
                .border_l_2()
                .border_color(p.accent)
                .bg(p.panel)
                .child(body);
            group = group.child(if let Some(action) = &item.action {
                let a = action.clone();
                tile.cursor_pointer()
                    .tab_index(0)
                    .hover(move |s| s.bg(p.accent.opacity(0.13)))
                    .focus(move |s| s.bg(p.accent.opacity(0.17)))
                    .on_click(cx.listener(move |s, _, w, cx| s.perform(&a.request, w, cx)))
            } else {
                tile
            });
        }
        group
    }
    fn content(&self, v: &View, modal: bool, compact: bool, cx: &mut Cx) -> Div {
        let p = self.palette;
        let mut content = column().w_full().gap_4();
        let title = if v.title.is_empty() {
            SharedString::new_static(PAGES[self.page].1)
        } else {
            v.title.clone()
        };
        content = content.child(
            column()
                .gap_1()
                .child(tag(
                    if modal { "DETAILS" } else { "USAGE REPORT" },
                    p.accent,
                ))
                .child(heading(title, p, if compact { 26. } else { 30. }))
                .when(!v.subtitle.is_empty(), |s| {
                    s.child(line(v.subtitle.clone(), p.secondary, 15.).w_full())
                }),
        );
        if !v.status.is_empty() {
            let ink = p.ink(&v.tone);
            content = content.child(
                row()
                    .items_start()
                    .gap_2()
                    .px_3()
                    .py_2()
                    .rounded_md()
                    .bg(ink.opacity(0.09))
                    .child(
                        div()
                            .mt(px(7.))
                            .size(px(7.))
                            .flex_shrink_0()
                            .rounded_full()
                            .bg(ink),
                    )
                    .child(line(v.status.clone(), ink, 13.).flex_1()),
            );
        }
        if !v.highlights.is_empty() {
            content = content.child(self.highlights(&v.highlights, compact, cx));
        }
        let expanded = self
            .layers
            .last()
            .filter(|_| modal)
            .map(|s| &s.expanded)
            .unwrap_or(&self.expanded);
        for (i, sec) in v.sections.iter().enumerate() {
            content = content.child(self.section(i, sec, expanded.contains(&i), modal, cx));
        }
        if !v.buttons.is_empty() {
            let mut buttons = row().flex_wrap().gap_2().child(
                button("cancel", p, p.text)
                    .child("Cancel")
                    .on_click(cx.listener(|s, _, w, cx| s.back(w, cx))),
            );
            for (i, a) in v.buttons.iter().enumerate() {
                buttons = buttons.child(self.action_button(("affirm", i), a, &v.tone, cx));
            }
            content = content.child(buttons);
        }
        if self.stopped {
            content = content.child(
                button("close", p, p.accent)
                    .child("Close dashboard")
                    .on_click(|_, _, cx| cx.quit()),
            );
        }
        content
    }
    fn layer_body(&self, s: &Surface, compact: bool, cx: &mut Cx) -> Div {
        let p = self.palette;
        match &s.layer {
            Layer::Detail(v) => self.content(v, true, compact, cx),
            Layer::Pages => column()
                .gap_3()
                .child(heading("Pages", p, 24.))
                .child(self.nav(false, cx)),
            Layer::Period => column()
                .gap_4()
                .child(heading("Reporting period", p, 24.))
                .child(line(
                    "Includes today. Daily totals use UTC, and missing time is never filled in.",
                    p.secondary,
                    15.,
                ))
                .child(
                    row()
                        .flex_wrap()
                        .gap_2()
                        .children(DAYS.into_iter().map(|d| {
                            button(("period", usize::from(d)), p, p.accent)
                                .when(self.days == d, |s| s.border_color(p.accent))
                                .child(format!("{d} days"))
                                .on_click(cx.listener(move |s, _, w, cx| {
                                    s.days = d;
                                    s.document = Arc::default();
                                    s.go(s.page, 0, w, cx);
                                }))
                        })),
                ),
            Layer::Keys => column()
                .gap_3()
                .child(heading("Keyboard shortcuts", p, 24.))
                .children(
                    [
                        ("Tab / Shift+Tab", "Move between controls"),
                        ("Enter / Space", "Activate the focused control"),
                        ("Alt+1 through Alt+7", "Switch pages"),
                        ("R", "Refresh local evidence"),
                        ("Esc", "Go back or cancel"),
                        ("Q", "Close the dashboard"),
                    ]
                    .into_iter()
                    .map(|(k, v)| {
                        row()
                            .gap_4()
                            .child(heading(k, p, 15.))
                            .child(line(v, p.secondary, 15.))
                    }),
                ),
            Layer::Input { key, field, .. } => {
                let search = key == "search";
                column().gap_4().child(heading(if search{"Search this list"}else{"Research a public tool"},p,24.)).child(line(if search{"Enter a name or evidence state. Leave this empty to see all results."}else{"Enter only the public tool name. You’ll see the exact query before anything opens in your browser."},p.secondary,15.)).child(field.clone()).child(row().gap_2().child(button("cancel-input",p,p.secondary).child("Cancel").on_click(cx.listener(|s,_,w,cx|s.back(w,cx)))).child(button("submit-input",p,p.accent).child(if search{"Apply search"}else{"Preview query"}).on_click(cx.listener(|s,_,w,cx|s.input_done(w,cx)))))
            }
        }
    }
}
impl Render for Dashboard {
    fn render(&mut self, w: &mut Window, cx: &mut Cx) -> impl IntoElement {
        let p = self.palette;
        let compact = w.viewport_size().width < px(770.);
        let modal = self.layers.last();
        let mut header = row()
            .w_full()
            .flex_wrap()
            .justify_between()
            .gap_2()
            .px_4()
            .py_2()
            .bg(p.panel)
            .border_b_1()
            .border_color(p.edge);
        if modal.is_some() {
            header = header
                .child(
                    button("back", p, p.secondary)
                        .child(if self.stopped {
                            "Close dashboard"
                        } else {
                            "‹  Back"
                        })
                        .on_click(cx.listener(|s, _, w, cx| s.back(w, cx))),
                )
                .child(line("Oma Usage", p.secondary, 14.));
        } else {
            header = header
                .child(if compact {
                    button("pages", p, p.accent)
                        .child("Pages")
                        .on_click(cx.listener(|s, _, w, cx| s.show(Layer::Pages, w, cx)))
                        .into_any_element()
                } else {
                    line("Workspace  /  Local evidence", p.secondary, 13.)
                        .flex_1()
                        .into_any_element()
                })
                .child(
                    row()
                        .gap_2()
                        .child(
                            button("window", p, p.text)
                                .child(format!("{} UTC days ▾", self.days))
                                .on_click(cx.listener(|s, _, w, cx| s.show(Layer::Period, w, cx))),
                        )
                        .child(
                            button("refresh", p, p.accent)
                                .child("Refresh")
                                .on_click(cx.listener(|s, _, w, cx| s.refresh(w, cx))),
                        ),
                );
        }
        let mut body = column().flex_1().min_h_0().child(header);
        if let Some((msg, tone)) = &self.notice {
            let ink = p.ink(tone);
            body = body.child(
                row()
                    .flex_wrap()
                    .px_4()
                    .py_2()
                    .gap_2()
                    .justify_between()
                    .bg(ink.opacity(0.13))
                    .child(line(msg.clone(), ink, 14.).flex_1())
                    .child(
                        button("dismiss", p, ink)
                            .child("Dismiss")
                            .on_click(cx.listener(|s, _, _, cx| {
                                s.notice = None;
                                cx.notify();
                            })),
                    ),
            );
        }
        let scroll = modal.map(|m| &m.scroll).unwrap_or(&self.scroll);
        let body_content = if let Some(m) = modal {
            self.layer_body(m, compact, cx)
        } else {
            self.content(&self.document, false, compact, cx)
        };
        let inset = if compact { px(12.) } else { px(26.) };
        let rail = if !compact && modal.is_none() {
            px(188.)
        } else {
            px(0.)
        };
        // Long paths and confirmation text must wrap inside the reading column,
        // never determine the scroll area's intrinsic width.
        let reading_width = (w.viewport_size().width - rail - inset * 2.).clamp(px(1.), px(1100.));
        body = body.child(
            div()
                .id("document-scroll")
                .track_scroll(scroll)
                .flex_1()
                .min_h_0()
                .min_w_0()
                .w_full()
                .overflow_x_hidden()
                .overflow_y_scroll()
                .child(
                    div()
                        .flex()
                        .flex_col()
                        .items_center()
                        .w_full()
                        .px(inset)
                        .py_5()
                        .child(div().w(reading_width).child(body_content)),
                ),
        );
        if self.busy == Busy::Acting
            || (self.busy == Busy::Reading && self.document.title.is_empty())
        {
            body = body.child(
                line(
                    if self.busy == Busy::Acting {
                        "Working…"
                    } else {
                        "Reading local evidence…"
                    },
                    p.accent,
                    13.,
                )
                .px_4()
                .py_1(),
            );
        }
        div()
            .flex()
            .flex_row()
            .size_full()
            .bg(p.bg)
            .text_color(p.text)
            .font_family("sans-serif")
            .track_focus(&self.root)
            .on_key_down(cx.listener(Self::key))
            .when(!compact && modal.is_none(), |s| s.child(self.nav(true, cx)))
            .child(body)
    }
}
