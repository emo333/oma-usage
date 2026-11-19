mod input;
mod render;
mod theme;

use gpui::{prelude::*, *};
use oma_usage_dashboard::{View, request};
use serde_json::{Map, Value, json};
use std::{
    borrow::Cow,
    collections::{HashMap, HashSet},
    io::{self, BufRead},
    path::PathBuf,
    sync::Arc,
    time::Duration,
};
use theme::Palette;

const PAGES: [(&str, &str); 7] = [
    ("overview", "Overview"),
    ("apps", "Applications"),
    ("commands", "Commands"),
    ("services", "Services"),
    ("review", "Package review"),
    ("alternatives", "Alternatives"),
    ("settings", "Settings"),
];
const DAYS: [u16; 4] = [7, 30, 90, 365];
struct Assets;
impl AssetSource for Assets {
    fn load(&self, path: &str) -> anyhow::Result<Option<Cow<'static, [u8]>>> {
        Ok(match path {
            "info.svg" => Some(Cow::Borrowed(include_bytes!("../assets/info.svg"))),
            "usage.svg" => Some(Cow::Borrowed(include_bytes!("../assets/usage.svg"))),
            _ => None,
        })
    }
    fn list(&self, _: &str) -> anyhow::Result<Vec<SharedString>> {
        Ok(vec!["info.svg".into(), "usage.svg".into()])
    }
}

enum Layer {
    Detail(Arc<View>),
    Pages,
    Keys,
    Period,
    Input {
        key: String,
        next: Value,
        field: Entity<input::Field>,
    },
}
struct Surface {
    layer: Layer,
    scroll: ScrollHandle,
    expanded: HashSet<usize>,
}
impl Surface {
    fn new(layer: Layer) -> Self {
        Self {
            layer,
            scroll: ScrollHandle::new(),
            expanded: HashSet::new(),
        }
    }
}
#[derive(Clone, Copy, PartialEq)]
enum Busy {
    Idle,
    Reading,
    Acting,
}
struct Dashboard {
    python: Arc<PathBuf>,
    palette: Palette,
    root: FocusHandle,
    page: usize,
    days: u16,
    offset: usize,
    filters: HashMap<usize, Map<String, Value>>,
    document: Arc<View>,
    layers: Vec<Surface>,
    scroll: ScrollHandle,
    expanded: HashSet<usize>,
    busy: Busy,
    pending: Option<Value>,
    notice: Option<(SharedString, SharedString)>,
    stopped: bool,
}
impl Dashboard {
    fn new(python: PathBuf, w: &mut Window, cx: &mut Context<Self>) -> Self {
        let root = cx.focus_handle();
        root.focus(w);
        let mut this = Self {
            python: Arc::new(python),
            palette: Palette::load(),
            root,
            page: 0,
            days: 30,
            offset: 0,
            filters: HashMap::new(),
            document: Arc::default(),
            layers: vec![],
            scroll: ScrollHandle::new(),
            expanded: HashSet::new(),
            busy: Busy::Idle,
            pending: None,
            notice: None,
            stopped: false,
        };
        let view = cx.weak_entity();
        w.on_window_should_close(cx, move |_, cx| {
            view.upgrade()
                .is_none_or(|v| v.read(cx).busy != Busy::Acting)
        });
        let (activate, receive) = async_channel::bounded(1);
        std::thread::spawn(move || {
            for line in io::stdin().lock().lines().map_while(Result::ok) {
                if line == "activate" {
                    let _ = activate.try_send(());
                }
            }
        });
        cx.spawn_in(w, async move |_, cx| {
            while receive.recv().await.is_ok() {
                if cx.update(|w, _| w.activate_window()).is_err() {
                    break;
                }
            }
        })
        .detach();
        cx.spawn_in(w, async move |view, cx| {
            loop {
                Timer::after(Duration::from_secs(5)).await;
                if view
                    .update_in(cx, |s, w, cx| {
                        if w.is_window_active() && !s.stopped {
                            s.palette = Palette::load();
                            if s.layers.is_empty() && s.busy == Busy::Idle {
                                s.refresh(w, cx);
                            } else {
                                cx.notify();
                            }
                        }
                    })
                    .is_err()
                {
                    break;
                }
            }
        })
        .detach();
        this.refresh(w, cx);
        this
    }
    fn page_request(&self) -> Value {
        json!({"op":"page","page":PAGES[self.page].0,"offset":self.offset})
    }
    fn refresh(&mut self, w: &mut Window, cx: &mut Context<Self>) {
        self.send(self.page_request(), w, cx);
    }
    fn go(&mut self, page: usize, offset: usize, w: &mut Window, cx: &mut Context<Self>) {
        if self.busy == Busy::Acting || self.stopped {
            return;
        }
        let changed = page != self.page || offset != self.offset;
        self.page = page;
        self.offset = offset;
        self.layers.clear();
        self.expanded.clear();
        self.scroll = ScrollHandle::new();
        self.root.focus(w);
        if changed {
            self.document = Arc::default();
            self.notice = None;
        }
        self.refresh(w, cx);
    }
    fn filter(&mut self, key: &str, value: Value) {
        self.filters
            .entry(self.page)
            .or_default()
            .insert(key.to_string(), value);
        self.offset = 0;
    }
    fn show(&mut self, layer: Layer, w: &mut Window, cx: &mut Context<Self>) {
        if self.busy == Busy::Acting {
            return;
        }
        self.root.focus(w);
        self.layers.push(Surface::new(layer));
        cx.notify();
    }
    fn back(&mut self, w: &mut Window, cx: &mut Context<Self>) {
        if self.busy == Busy::Acting {
            return;
        }
        if self.stopped {
            cx.quit();
            return;
        }
        self.layers.pop();
        self.root.focus(w);
        cx.notify();
    }
    fn input_done(&mut self, w: &mut Window, cx: &mut Context<Self>) {
        if self.busy == Busy::Acting
            || !matches!(
                self.layers.last().map(|s| &s.layer),
                Some(Layer::Input { .. })
            )
        {
            return;
        }
        let Some(Surface {
            layer: Layer::Input { key, next, field },
            ..
        }) = self.layers.pop()
        else {
            return;
        };
        let text = field.read(cx).text.trim().to_owned();
        self.filter(&key, Value::String(text));
        self.root.focus(w);
        self.perform(&next, w, cx);
    }
    fn perform(&mut self, r: &Value, w: &mut Window, cx: &mut Context<Self>) {
        if self.stopped || self.busy == Busy::Acting {
            return;
        }
        // Never overlap a picker or a state-changing request with an in-flight read.
        // Latch the user's action immediately, so a double-click cannot enqueue it twice.
        if self.busy == Busy::Reading
            && !matches!(
                r["op"].as_str(),
                Some("page" | "input" | "reset_history_file")
            )
        {
            self.pending = Some(r.clone());
            self.busy = Busy::Acting;
            cx.notify();
            return;
        }
        match r["op"].as_str() {
            Some("page") => {
                if let Some(i) = PAGES.iter().position(|p| Some(p.0) == r["page"].as_str()) {
                    self.go(i, r["offset"].as_u64().unwrap_or(0) as usize, w, cx)
                }
            }
            Some("input") => {
                if let Some(key) = r["key"].as_str() {
                    let text = self
                        .filters
                        .get(&self.page)
                        .and_then(|f| f.get(key))
                        .and_then(Value::as_str)
                        .unwrap_or("");
                    let field = cx.new(|cx| input::Field::new(text.into(), self.palette, cx));
                    self.show(
                        Layer::Input {
                            key: key.into(),
                            next: r["next"].clone(),
                            field: field.clone(),
                        },
                        w,
                        cx,
                    );
                    field.read(cx).focus.focus(w);
                }
            }
            Some("reset_history_file") => {
                self.filter("history_file", json!(""));
                self.refresh(w, cx);
            }
            Some("choose_file") => {
                let result = cx.prompt_for_paths(PathPromptOptions {
                    files: true,
                    directories: false,
                    multiple: false,
                    prompt: Some("Choose Bash history file".into()),
                });
                self.busy = Busy::Acting;
                cx.spawn_in(w, async move |view, cx| {
                    let result = result.await;
                    let _ = view.update_in(cx, |s, w, cx| {
                        s.busy = Busy::Idle;
                        match result {
                            Ok(Ok(Some(paths))) => {
                                if let Some(path) = paths.first().and_then(|p| p.to_str()) {
                                    s.filter("history_file", json!(path));
                                    s.refresh(w, cx);
                                }
                            }
                            Ok(Ok(None)) => (),
                            _ => {
                                s.notice = Some((
                                    "Could not open the desktop file chooser.".into(),
                                    "warn".into(),
                                ))
                            }
                        };
                        cx.notify();
                    });
                })
                .detach();
                cx.notify();
            }
            Some(_) => self.send(r.clone(), w, cx),
            None => (),
        }
    }
    fn send(&mut self, mut r: Value, w: &mut Window, cx: &mut Context<Self>) {
        if self.stopped || self.busy == Busy::Acting {
            return;
        }
        if self.busy == Busy::Reading {
            self.pending = Some(r);
            cx.notify();
            return;
        }
        let is_page = r["op"] == "page";
        let can_open = r["op"] == "open_url";
        self.busy = if is_page { Busy::Reading } else { Busy::Acting };
        r["days"] = json!(self.days);
        if let Some(filters) = self.filters.get(&self.page) {
            r["filters"] = Value::Object(filters.clone());
        }
        let python = self.python.clone();
        let work = cx
            .background_executor()
            .spawn(async move { request(&python, r) });
        cx.spawn_in(w, async move |view, cx| {
            let result = work.await;
            let _ = view.update_in(cx, |s, w, cx| {
                s.busy = Busy::Idle;
                if let Some(next) = s.pending.take() {
                    if next["op"] == "page" {
                        s.send(next, w, cx)
                    } else {
                        s.perform(&next, w, cx)
                    }
                    return;
                }
                match result {
                    Err(error) => {
                        s.notice = Some((
                            if is_page {
                                error.into()
                            } else {
                                format!("{error} Check the current state before retrying.").into()
                            },
                            "warn".into(),
                        ))
                    }
                    Ok(v) if !is_page && v.close => {
                        s.stopped = true;
                        s.layers.clear();
                        s.show(Layer::Detail(Arc::new(v)), w, cx);
                    }
                    Ok(v) if can_open && v.url.is_some() => {
                        if let Some(url) = v.url.filter(|url| url.starts_with("https://")) {
                            cx.open_url(&url);
                            s.back(w, cx);
                        }
                    }
                    Ok(v) if !v.notice.is_empty() => {
                        s.notice = Some((v.notice, v.tone));
                        if !is_page {
                            s.layers.clear();
                            s.root.focus(w);
                            s.refresh(w, cx);
                        }
                    }
                    Ok(v) if is_page => s.document = Arc::new(v),
                    Ok(v) => s.show(Layer::Detail(Arc::new(v)), w, cx),
                }
                cx.notify();
            });
        })
        .detach();
        cx.notify();
    }
    fn key(&mut self, e: &KeyDownEvent, w: &mut Window, cx: &mut Context<Self>) {
        let k = &e.keystroke;
        match k.key.as_str() {
            "tab" => {
                if k.modifiers.shift {
                    w.focus_prev()
                } else {
                    w.focus_next()
                }
            }
            "escape" if !self.layers.is_empty() => self.back(w, cx),
            "enter" if matches!(self.layers.last().map(|s|&s.layer),Some(Layer::Input{field,..}) if field.read(cx).focus.is_focused(w)) => {
                self.input_done(w, cx)
            }
            _ if matches!(
                self.layers.last().map(|s| &s.layer),
                Some(Layer::Input { .. })
            ) =>
            {
                return;
            }
            "q" if self.busy != Busy::Acting => cx.quit(),
            "r" if self.layers.is_empty() && self.busy == Busy::Idle => self.refresh(w, cx),
            "?" | "question" if self.busy != Busy::Acting => self.show(Layer::Keys, w, cx),
            key if k.modifiers.alt && self.layers.is_empty() => {
                if let Ok(i @ 1..=7) = key.parse::<usize>() {
                    self.go(i - 1, 0, w, cx)
                }
            }
            _ => return,
        }
        cx.stop_propagation();
    }
}
fn main() {
    let Some(python) = std::env::args_os()
        .nth(1)
        .map(PathBuf::from)
        .filter(|p| p.is_absolute())
    else {
        eprintln!("Start Oma Usage using its installed launcher.");
        std::process::exit(2)
    };
    Application::new().with_assets(Assets).run(move |cx| {
        cx.on_window_closed(|cx| {
            if cx.windows().is_empty() {
                cx.quit()
            }
        })
        .detach();
        let bounds = Bounds::centered(None, size(px(1160.), px(790.)), cx);
        let result = cx.open_window(
            WindowOptions {
                app_id: Some("org.omarchy.OmaUsage".into()),
                window_bounds: Some(WindowBounds::Windowed(bounds)),
                window_min_size: Some(size(px(360.), px(320.))),
                titlebar: Some(TitlebarOptions {
                    title: Some("Oma Usage".into()),
                    ..Default::default()
                }),
                ..Default::default()
            },
            |w, cx| cx.new(|cx| Dashboard::new(python, w, cx)),
        );
        if result.is_err() {
            eprintln!("Cannot open the GPUI window. Check the desktop session and Vulkan driver.");
            std::process::exit(1);
        }
        cx.activate(true);
    });
}
