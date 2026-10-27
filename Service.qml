import QtQuick
import Quickshell
import Quickshell.Wayland
import Quickshell.Io

Item {
    id: root
    // An override permits embedding this real component in another shell instance.
    property string socketPath: Quickshell.env("XDG_RUNTIME_DIR")
        ? Quickshell.env("XDG_RUNTIME_DIR") + "/oma-usage/collector.sock" : ""
    property bool confirmedEnabled: false
    property int idleSeconds: 60
    property string observerId: Date.now().toString(36) + "-" + Math.random().toString(36).slice(2)
    property int sequence: 0
    readonly property bool collecting: socket.connected && confirmedEnabled
    readonly property var active: collecting ? ToplevelManager.activeToplevel : null
    readonly property string currentAppId: collecting && active ? String(active.appId || "") : ""

    function send(message) {
        if (!socket.connected) return
        socket.write(JSON.stringify(message) + "\n")
        socket.flush()
    }

    function snapshot() {
        if (!collecting) return
        var name = currentAppId
        // Do not transmit paths or unsafe identities. Never read a window title.
        if (/[\/\u0000-\u001f\u007f-\u009f]/.test(name)
            || encodeURIComponent(name).replace(/%[0-9A-F]{2}/g, "x").length > 255) name = ""
        send({op: "focus", observer_id: observerId, seq: ++sequence,
              app_id: name, idle: idleMonitor.isIdle})
    }

    onCurrentAppIdChanged: snapshot()
    onCollectingChanged: {
        if (collecting) Qt.callLater(snapshot)
    }

    IdleMonitor {
        id: idleMonitor
        enabled: root.collecting
        timeout: root.idleSeconds
        respectInhibitors: false
        onIsIdleChanged: root.snapshot()
    }

    Socket {
        id: socket
        path: root.socketPath
        connected: false
        onConnectionStateChanged: {
            root.confirmedEnabled = false
            if (connected) root.send({op: "hello", role: "observer"})
        }
        parser: SplitParser {
            splitMarker: "\n"
            onRead: function(line) {
                try {
                    var message = JSON.parse(line)
                    if (message.op !== "settings") return
                    if (typeof message.collection_enabled !== "boolean"
                        || [60, 120, 300].indexOf(message.idle_seconds) < 0) return
                    root.idleSeconds = message.idle_seconds
                    root.confirmedEnabled = message.collection_enabled
                } catch (ignored) {
                    // Invalid replies never enable collection or reveal payloads.
                }
            }
        }
    }

    Timer {
        id: reconnect
        interval: 5000
        repeat: true
        running: true
        triggeredOnStart: true
        onTriggered: if (!socket.connected && root.socketPath) socket.connected = true
    }
    Timer {
        id: heartbeat
        interval: 5000
        repeat: true
        running: root.collecting
        onTriggered: root.snapshot()
    }
    Component.onDestruction: socket.connected = false
}
