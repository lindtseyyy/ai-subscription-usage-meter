const Applet = imports.ui.applet;
const Main = imports.ui.main;
const Settings = imports.ui.settings;
const Tooltips = imports.ui.tooltips;
const Gio = imports.gi.Gio;
const GLib = imports.gi.GLib;
const St = imports.gi.St;
const Clutter = imports.gi.Clutter;
const Gtk = imports.gi.Gtk;
const Pango = imports.gi.Pango;

const REFRESH_SECONDS = 180;
const SERVICES = [["claude", "Claude"], ["codex", "Codex"], ["commandcode", "CommandCode"], ["agy", "Antigravity"], ["mistral", "Mistral"]];
const WINDOWS = [["five_hour", "5h", "5-hour"], ["weekly", "W", "Weekly"]];

// Green while there's plenty left, shifting to red as it nears 100%.
function colorFor(pct) {
    if (pct >= 90) return "#ef5350";
    if (pct >= 75) return "#ffa726";
    if (pct >= 50) return "#fdd835";
    return "#66bb6a";
}

// St styles survive the panel theme; Pango colour markup does not.
function lbl(text, style) {
    return new St.Label({ text: String(text), style: style || "", y_align: Clutter.ActorAlign.CENTER });
}

// Long text (errors) wraps instead of stretching a column.
function wrapLbl(text, style) {
    let l = lbl(text, style);
    l.clutter_text.line_wrap = true;
    l.clutter_text.ellipsize = Pango.EllipsizeMode.NONE;
    l.clutter_text.line_wrap_mode = Pango.WrapMode.WORD_CHAR;
    return l;
}

function row(...children) {
    let box = new St.BoxLayout({ style: "spacing: 4px;" });
    for (let c of children) box.add_child(c);
    return box;
}

function untilText(epoch) {
    if (!epoch) return "";
    let secs = epoch - Math.floor(Date.now() / 1000);
    if (secs <= 0) return "resetting now";
    let d = Math.floor(secs / 86400), h = Math.floor(secs % 86400 / 3600), m = Math.floor(secs % 3600 / 60);
    let rel = d > 0 ? `${d}d ${h}h` : h > 0 ? `${h}h ${String(m).padStart(2, "0")}m` : `${m}m`;
    let at = GLib.DateTime.new_from_unix_local(epoch).format(d > 0 ? "%a %H:%M" : "%H:%M");
    return `resets in ${rel} (${at})`;
}

function bar(pct, color, n) {
    let filled = Math.round(Math.min(pct, 100) / 100 * n);
    let box = new St.BoxLayout();
    box.add_child(lbl("█".repeat(filled), `color: ${color};`));
    box.add_child(lbl("█".repeat(n - filled), "color: rgba(128,128,128,0.35);"));
    return box;
}

function fmt(n) {
    return n != null ? +Number(n).toFixed(2) : "";
}

const DIM = "color: rgba(200,200,200,0.65);";
const WARN = "color: #ffa726;";

class UsageMeterApplet extends Applet.IconApplet {
    constructor(metadata, orientation, panelHeight, instanceId) {
        super(orientation, panelHeight, instanceId);
        this.script = GLib.build_filenamev([metadata.path, "usage_fetch.py"]);
        this.iconDir = GLib.build_filenamev([metadata.path, "icons"]);
        this.data = null;
        this.loaded = false;
        this.busy = false;
        this.timers = [];

        this.hotkeyName = `${metadata.uuid}-drawer-${instanceId}`;
        this.settings = new Settings.AppletSettings(this, metadata.uuid, instanceId);
        this.settings.bind("toggle-shortcut", "toggleShortcut", () => this._bindShortcut());
        for (let [key] of SERVICES) this.settings.bind("show-" + key, "show_" + key, () => this._onServicesChanged());
        this.set_applet_icon_symbolic_name("utilities-system-monitor-symbolic");
        this._createDrawer();
        this._bindShortcut();
        this._monitorSignal = Main.layoutManager.connect("monitors-changed", () => this._closeDrawer());
        this._workareaSignal = global.display.connect("workareas-changed", () => {
            if (this.isOpen) this._positionDrawer();
        });
        this._refresh();
        this._every(REFRESH_SECONDS, () => this._refresh());
    }

    _enabled() {
        return SERVICES.filter(([key]) => this["show_" + key]);
    }

    _onServicesChanged() {
        this._rebuildIfOpen();
        this._refresh(); // picks up a service that was just switched on
    }

    _rebuildIfOpen() {
        if (this.isOpen) this._buildBody();
    }

    _every(seconds, fn) {
        this.timers.push(GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, seconds, () => {
            fn();
            return GLib.SOURCE_CONTINUE;
        }));
    }

    // Returns null when there's no icons/<key>.png, so callers can fall back to the name.
    _logo(key, size) {
        let file = Gio.File.new_for_path(GLib.build_filenamev([this.iconDir, key + ".png"]));
        if (!file.query_exists(null)) return null;
        return new St.Icon({ gicon: new Gio.FileIcon({ file }), icon_size: size, y_align: Clutter.ActorAlign.CENTER });
    }

    _refresh() {
        if (this.destroyed) return;
        let keys = this._enabled().map(([key]) => key);
        if (this.busy) return;
        if (!keys.length) { // nothing selected
            this.data = { updated: Math.floor(Date.now() / 1000) };
            this.loaded = true;
            this._rebuildIfOpen();
            return;
        }
        this.busy = true;
        this._rebuildIfOpen();
        try {
            let argv = ["python3", "-I", this.script].concat(keys);
            let proc = Gio.Subprocess.new(argv, Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE);
            proc.communicate_utf8_async(null, null, (p, res) => {
                if (this.destroyed) return;
                this.busy = false;
                try {
                    let [, out] = p.communicate_utf8_finish(res);
                    this.data = Object.assign(this.data || {}, JSON.parse(out));
                } catch (e) {
                    global.logError("usage-meter: " + e);
                    this.data = null;
                }
                this.loaded = true;
                this._rebuildIfOpen();
            });
        } catch (e) {
            this.busy = false;
            global.logError("usage-meter: " + e);
            this.loaded = true;
            this._rebuildIfOpen();
        }
    }

    _bindShortcut() {
        Main.keybindingManager.removeHotKey(this.hotkeyName);
        let shortcut = this.toggleShortcut || "";
        let registered = !shortcut || Main.keybindingManager.addHotKey(this.hotkeyName, shortcut, () => this._toggleDrawer());
        let description = shortcut.replace(/<([^>]+)>/g, "$1+").replace(/::/g, " / ");
        let name = "AI Usage Meter" + (description ? ` (${description})` : "");
        this.actor.accessible_name = name;
        this.set_applet_tooltip(name + (registered ? "" : " — shortcut unavailable; choose another in Settings"));
        if (!registered) Main.notifyError("AI Usage Meter", `Cannot register ${description}. It may already be in use. Choose another shortcut in the applet settings.`);
    }

    _button(text, action, iconName) {
        let button = new St.Button({ style_class: "notification-button", can_focus: true, reactive: true, accessible_name: text });
        button.set_child(new St.Icon({ icon_name: iconName, icon_type: St.IconType.SYMBOLIC, icon_size: 16 }));
        new Tooltips.Tooltip(button, text);
        button.connect("clicked", action);
        return button;
    }

    _createDrawer() {
        this.overlay = new St.Widget({ reactive: true, visible: false });
        this.drawer = new St.BoxLayout({ vertical: true, reactive: true, style_class: "dialog modal-dialog", style: "padding: 16px; spacing: 12px; border-radius: 0;" });
        this.overlay.add_child(this.drawer);
        let header = new St.BoxLayout({ style: "spacing: 6px;" });
        header.add(lbl("AI Usage Meter", "font-weight: bold; font-size: 1.2em;"), { expand: true });
        header.add_child(this._button("Refresh now", () => this._refresh(), "view-refresh-symbolic"));
        header.add_child(this._button("Settings", () => {
            this._closeDrawer();
            this.configureApplet();
        }, "emblem-system-symbolic"));
        this.closeButton = this._button("Close", () => this._closeDrawer(), "window-close-symbolic");
        header.add_child(this.closeButton);
        this.drawer.add_child(header);
        this.status = wrapLbl("", DIM + " font-size: 0.85em;");
        this.drawer.add_child(this.status);
        this.scroll = new St.ScrollView({ style_class: "vfade", can_focus: true, reactive: true, x_expand: true, y_expand: true });
        this.scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC);
        this.body = new St.BoxLayout({ vertical: true, style: "spacing: 18px;" });
        this.scroll.add_actor(this.body);
        this.drawer.add(this.scroll, { expand: true, x_fill: true, y_fill: true });
        this.overlay.connect("button-press-event", (_actor, event) => {
            if (!this.drawer.contains(event.get_source())) {
                this._closeDrawer();
                return Clutter.EVENT_STOP;
            }
            return Clutter.EVENT_PROPAGATE;
        });
        this.overlay.connect("key-press-event", (_actor, event) => {
            let key = event.get_key_symbol();
            if (key === Clutter.KEY_Escape) {
                this._closeDrawer();
                return Clutter.EVENT_STOP;
            }
            if (key === Clutter.KEY_Tab || key === Clutter.KEY_ISO_Left_Tab) {
                this.drawer.navigate_focus(global.stage.get_key_focus(),
                    event.get_state() & Clutter.ModifierType.SHIFT_MASK ? Gtk.DirectionType.TAB_BACKWARD : Gtk.DirectionType.TAB_FORWARD, true);
                return Clutter.EVENT_STOP;
            }
            let adjustment = this.scroll.vscroll.adjustment;
            let delta = key === Clutter.KEY_Down ? adjustment.step_increment :
                key === Clutter.KEY_Up ? -adjustment.step_increment :
                key === Clutter.KEY_Page_Down ? adjustment.page_increment :
                key === Clutter.KEY_Page_Up ? -adjustment.page_increment : 0;
            if (delta || key === Clutter.KEY_Home || key === Clutter.KEY_End) {
                adjustment.value = Math.max(adjustment.lower, Math.min(adjustment.upper - adjustment.page_size,
                    key === Clutter.KEY_Home ? adjustment.lower : key === Clutter.KEY_End ? adjustment.upper : adjustment.value + delta));
                return Clutter.EVENT_STOP;
            }
            return Clutter.EVENT_PROPAGATE;
        });
        Main.layoutManager.addChrome(this.overlay, { affectsStruts: false, visibleInFullscreen: true });
    }

    _positionDrawer() {
        this.overlay.set_position(0, 0);
        this.overlay.set_size(global.screen_width, global.screen_height);
        let area = global.workspace_manager.get_active_workspace().get_work_area_for_monitor(this.monitorIndex);
        let width = Math.min(420, area.width);
        this.drawer.set_position(area.x + area.width - width, area.y);
        this.drawer.set_size(width, area.height);
    }

    _toggleDrawer() {
        if (this.isOpen) {
            this._closeDrawer();
            return;
        }
        let extended = Main.layoutManager.monitors.find(m => m.index !== Main.layoutManager.primaryIndex);
        this.monitorIndex = extended ? extended.index : Main.layoutManager.primaryIndex;
        this._positionDrawer();
        this._buildBody();
        this.overlay.show();
        if (!Main.pushModal(this.overlay)) {
            this.overlay.hide();
            Main.notifyError("AI Usage Meter", "Cannot open the sidebar while another system dialog is active.");
            return;
        }
        this.isOpen = true;
        this.closeButton.grab_key_focus();
        if (!this.loaded) this._refresh();
    }

    _closeDrawer() {
        if (!this.isOpen) return;
        this.isOpen = false;
        Main.popModal(this.overlay);
        this.overlay.hide();
    }

    _buildBody() {
        let adjustment = this.scroll.vscroll.adjustment;
        let position = adjustment.value;
        this.body.destroy_all_children();
        let services = this._enabled();
        if (!services.length) this.body.add_child(wrapLbl("No subscriptions selected. Choose services in Settings.", DIM));
        else if (!this.loaded && !this.data) this.body.add_child(lbl("Loading usage…", DIM));
        else if (!this.data) this.body.add_child(wrapLbl("Fetcher failed — see ~/.xsession-errors", WARN));
        else for (let [key, name] of services) this.body.add_child(this._serviceColumn(key, name, this.data[key] || {}));
        this.status.set_text(this.busy ? "Refreshing usage…" : this.data && this.data.updated ?
            "Updated " + GLib.DateTime.new_from_unix_local(this.data.updated).format("%H:%M:%S") : "");
        adjustment.value = position;
    }

    // Providers, model groups and accounts all follow the same vertical reading order.
    _serviceColumn(key, name, s) {
        let col = new St.BoxLayout({ vertical: true, style: "spacing: 8px; padding: 12px; background-color: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.14); border-radius: 10px;" });
        let head = [this._logo(key, 18), lbl(name, "font-weight: bold;")].filter(Boolean);
        if (s.plan) head.push(lbl(`(${s.plan})`, DIM));
        let heading = row(...head);
        heading.set_style("spacing: 6px; padding: 8px; background-color: rgba(255,255,255,0.09); border-radius: 6px;");
        col.add_child(heading);
        for (let a of s.groups || s.accounts || [s]) {
            let box = new St.BoxLayout({ vertical: true, style: "spacing: 6px;" });
            col.add_child(box);
            if (a.account) box.add_child(wrapLbl(a.account, s.accounts ? "font-weight: bold;" : DIM + " font-size: 0.85em;"));
            if (a.note) box.add_child(wrapLbl(a.note, DIM + " font-size: 0.85em;"));
            if (a.name) box.add_child(wrapLbl(a.name, "font-weight: bold;"));
            let quotas = a.quotas || WINDOWS.filter(([w]) => a[w]).map(([w, , label]) => Object.assign({ label }, a[w]));
            for (let quota of quotas) {
                let { pct, resets_at, label } = quota;
                let c = colorFor(pct);
                let block = new St.BoxLayout({ vertical: true });
                block.add_child(row(bar(pct, c, 10),
                    lbl((pct > 0 && pct < 10 ? pct.toFixed(1) : Math.round(pct)) + "%",
                        `color: ${c}; font-weight: bold; min-width: 3em; text-align: right;`),
                    lbl(s.groups ? label.replace(a.name + " ", "").replace("Five Hour Limit", "5-hour").replace("Weekly Limit", "Weekly") : label)));
                if (quota.limit != null) {
                    let used = quota.used != null ? fmt(quota.used) + " / " : "";
                    block.add_child(lbl(`${used}${fmt(quota.limit)} ${quota.unit || "credits"}`, DIM + " font-size: 0.85em;"));
                }
                block.add_child(lbl(untilText(resets_at), DIM + " font-size: 0.85em;"));
                box.add_child(block);
            }
            if (a.balance && a.balance.remaining != null) {
                box.add_child(lbl(`Balance: ${fmt(a.balance.remaining)} ${a.balance.unit || "credits"}`, DIM + " font-size: 0.85em;"));
            }
            if (a !== s && a.error) box.add_child(wrapLbl("⚠ " + a.error, WARN + " font-size: 0.85em;"));
        }
        if (s.error) {
            let when = s.fetched ? ` - showing data from ${GLib.DateTime.new_from_unix_local(s.fetched).format("%H:%M")}` : "";
            col.add_child(wrapLbl("⚠ " + s.error + when, WARN + " font-size: 0.85em;"));
        }
        return col;
    }

    on_applet_clicked() {
        this._toggleDrawer();
    }
    configureApplet(tab = 0) {
        this._closeDrawer();
        super.configureApplet(tab);
    }


    on_applet_removed_from_panel() {
        this.destroyed = true;
        this._closeDrawer();
        Main.keybindingManager.removeHotKey(this.hotkeyName);
        Main.layoutManager.disconnect(this._monitorSignal);
        global.display.disconnect(this._workareaSignal);
        Main.layoutManager.removeChrome(this.overlay);
        this.overlay.destroy();
        this.settings.finalize();
        for (let t of this.timers) GLib.source_remove(t);
        this.timers = [];
    }
}

function main(metadata, orientation, panelHeight, instanceId) {
    return new UsageMeterApplet(metadata, orientation, panelHeight, instanceId);
}
