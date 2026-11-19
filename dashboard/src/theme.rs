use gpui::{Hsla, rgb};
use std::{env, fs, path::PathBuf};

#[derive(Clone, Copy, PartialEq)]
pub struct Palette {
    pub bg: Hsla,
    pub panel: Hsla,
    pub rail: Hsla,
    pub edge: Hsla,
    pub text: Hsla,
    pub secondary: Hsla,
    pub accent: Hsla,
    pub ok: Hsla,
    pub warn: Hsla,
    pub danger: Hsla,
}

impl Palette {
    // Re-open on each refresh: Omarchy replaces the theme symlink atomically.
    pub fn load() -> Self {
        let state = env::var_os("XDG_STATE_HOME")
            .map(PathBuf::from)
            .or_else(|| env::var_os("HOME").map(|p| PathBuf::from(p).join(".local/state")));
        let source = state
            .and_then(|p| fs::read_to_string(p.join("omarchy/current/theme/colors.toml")).ok());
        let mut p = Self {
            bg: rgb(0x131820).into(),
            panel: rgb(0x1d2531).into(),
            rail: rgb(0x10151d).into(),
            edge: rgb(0x344050).into(),
            text: rgb(0xe4ebf4).into(),
            secondary: rgb(0xacb9cb).into(),
            accent: rgb(0x87bfff).into(),
            ok: rgb(0x87d8aa).into(),
            warn: rgb(0xf0cc84).into(),
            danger: rgb(0xffa39f).into(),
        };
        for line in source.as_deref().unwrap_or("").lines() {
            let Some((key, value)) = line.split_once('=') else {
                continue;
            };
            let Some(hex) = value
                .trim()
                .strip_prefix("\"#")
                .and_then(|s| s.strip_suffix('"'))
                .filter(|s| s.len() == 6)
            else {
                continue;
            };
            let Ok(value) = u32::from_str_radix(hex, 16) else {
                continue;
            };
            let field = match key.trim() {
                "background" => &mut p.bg,
                "lighter_background" => &mut p.panel,
                "dark_background" => &mut p.rail,
                "muted" => &mut p.edge,
                "bright_foreground" => &mut p.text,
                "foreground" => &mut p.secondary,
                "accent" => &mut p.accent,
                "green" => &mut p.ok,
                "yellow" => &mut p.warn,
                "red" => &mut p.danger,
                _ => continue,
            };
            *field = rgb(value).into();
        }
        p
    }
    pub fn ink(self, tone: &str) -> Hsla {
        match tone {
            "warn" => self.warn,
            "danger" => self.danger,
            "ok" => self.ok,
            _ => self.accent,
        }
    }
}
