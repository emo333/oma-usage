import QtQuick
import qs.Ui

BarWidget {
    id: root
    moduleName: "oma.usage"
    implicitWidth: button.implicitWidth
    implicitHeight: button.implicitHeight

    function quotePosix(value) {
        return "'" + value.replace(/'/g, "'\\''") + "'"
    }

    WidgetButton {
        id: button
        anchors.fill: parent
        bar: root.bar
        text: "Usage"
        tooltipText: "Local software usage insights"
        horizontalMargin: 7.5
        onPressed: function(button) {
            if (button !== Qt.LeftButton || !root.bar) return
            var url = Qt.resolvedUrl("bootstrap.py").toString()
            if (url.indexOf("file://") !== 0) return
            var path = decodeURIComponent(url.slice(7))
            if (path.charAt(0) === "/")
                root.bar.run("/usr/bin/python3 -I " + root.quotePosix(path))
        }
    }
}
