//! Data exchanged with the installed, local Python backend. Labels are plain text.
use gpui::SharedString;
use serde::Deserialize;
use serde_json::Value;
use std::io::Write;
use std::path::Path;
use std::process::{Command, Stdio};
use std::sync::Arc;

#[derive(Default, Deserialize)]
#[serde(default)]
pub struct View {
    pub title: SharedString,
    pub subtitle: SharedString,
    pub status: SharedString,
    pub tone: SharedString,
    pub highlights: Vec<Item>,
    pub sections: Vec<Section>,
    pub buttons: Vec<Arc<Button>>,
    pub notice: SharedString,
    pub close: bool,
    pub url: Option<String>,
}

#[derive(Default, Deserialize)]
#[serde(default)]
pub struct Section {
    pub title: SharedString,
    pub help: SharedString,
    pub subtitle: SharedString,
    pub tone: SharedString,
    pub items: Vec<Item>,
    pub buttons: Vec<Arc<Button>>,
    pub options: Vec<Options>,
}

#[derive(Deserialize)]
pub struct Button {
    pub label: SharedString,
    pub request: Value,
    #[serde(default)]
    pub tone: SharedString,
}

#[derive(Default, Deserialize)]
#[serde(default)]
pub struct Item {
    pub label: SharedString,
    pub value: SharedString,
    pub note: SharedString,
    pub tone: SharedString,
    pub action: Option<Arc<Button>>,
}

#[derive(Deserialize)]
pub struct Options {
    pub label: SharedString,
    pub key: SharedString,
    #[serde(default)]
    pub selected: SharedString,
    pub values: Vec<Choice>,
}

#[derive(Deserialize)]
pub struct Choice {
    pub label: SharedString,
    pub value: SharedString,
}

pub fn request(python: &Path, message: Value) -> Result<View, &'static str> {
    let mut child = Command::new(python)
        .args(["-I", "-m", "oma_usage.dashboard_bridge"])
        .current_dir("/")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|_| "Could not start the installed dashboard backend.")?;
    let sent = child.stdin.take().is_some_and(|mut pipe| {
        serde_json::to_writer(&mut pipe, &message).is_ok() && pipe.flush().is_ok()
    });
    let reply = child
        .wait_with_output()
        .map_err(|_| "The backend reply could not be read.")?;
    if !sent || !reply.status.success() {
        return Err("The dashboard backend exited without a usable reply.");
    }
    serde_json::from_slice(&reply.stdout)
        .map_err(|_| "The installed dashboard and backend returned incompatible data.")
}
