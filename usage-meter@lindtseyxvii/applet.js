const Applet = imports.ui.applet;
const PopupMenu = imports.ui.popupMenu;
const Settings = imports.ui.settings;
const Gio = imports.gi.Gio;
const GLib = imports.gi.GLib;
const St = imports.gi.St;
const Clutter = imports.gi.Clutter;

const REFRESH_SECONDS = 180;
const MUSE_REFRESH_SECONDS = 30; // local file read, so it can be quick
const SERVICES = [["claude", "Claude"], ["codex", "Codex"], ["agy", "Antigravity"], ["muse", "Muse"]];
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
    return l;
}

function row(...children) {
    let box = new St.BoxLayout({ style: "spacing: 4px;" });
    for (let c of children) box.add_child(c);
    return box;
}

function money(usd) {
    // Session costs are often fractions of a cent, so keep more digits when small.
    let dp = usd === 0 || usd >= 1 ? 2 : usd >= 0.01 ? 3 : 4;
    return "$" + usd.toFixed(dp);
}

function tokens(n) {
    return n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e3 ? (n / 1e3).toFixed(1) + "k" : String(n);
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

const DIM = "color: rgba(200,200,200,0.65);";
const WARN = "color: #ffa726;";

class UsageMeterApplet extends Applet.Applet {
    constructor(metadata, orientation, panelHeight, instanceId) {
        super(orientation, panelHeight, instanceId);
        this.script = GLib.build_filenamev([metadata.path, "usage_fetch.py"]);
        this.iconDir = GLib.build_filenamev([metadata.path, "icons"]);
        this.data = null;
        this.loaded = false;
        this.busy = {};
        this.timers = [];

        this.settings = new Settings.AppletSettings(this, metadata.uuid, instanceId);
        this.settings.bind("menu-layout", "menuLayout", () => this._rebuildIfOpen());
        for (let [key] of SERVICES) this.settings.bind("show-" + key, "show_" + key, () => this._onServicesChanged());

        this.box = new St.BoxLayout({ style: "spacing: 4px;", y_align: Clutter.ActorAlign.CENTER });
        this.actor.add_child(this.box);

        this.menuManager = new PopupMenu.PopupMenuManager(this);
        this.menu = new Applet.AppletPopupMenu(this, orientation);
        this.menuManager.addMenu(this.menu);

        this._setPanel([lbl("AI usage …", DIM)]);
        this.set_applet_tooltip("Loading usage…");
        this._refresh();
        this._every(REFRESH_SECONDS, () => this._refresh());
        this._every(MUSE_REFRESH_SECONDS, () => this._refresh("muse"));
    }

    _enabled() {
        return SERVICES.filter(([key]) => this["show_" + key]);
    }

    _onServicesChanged() {
        this._render();
        this._rebuildIfOpen();
        this._refresh(); // picks up a service that was just switched on
    }

    _rebuildIfOpen() {
        if (this.menu.isOpen) this._buildMenu();
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

    _setPanel(children) {
        this.box.destroy_all_children();
        for (let c of children) this.box.add_child(c);
    }

    // only: refresh a single source and merge it into the last full result.
    _refresh(only) {
        let tag = only || "all";
        let keys = this._enabled().map(([key]) => key).filter(k => !only || k === only);
        if (this.busy[tag] || (only && !this.data)) return;
        if (!keys.length) { // nothing selected (or this source is switched off)
            if (!only) {
                this.data = { updated: Math.floor(Date.now() / 1000) };
                this.loaded = true;
                this._render();
            }
            return;
        }
        this.busy[tag] = true;
        try {
            let argv = ["python3", "-I", this.script].concat(keys);
            let proc = Gio.Subprocess.new(argv, Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE);
            proc.communicate_utf8_async(null, null, (p, res) => {
                this.busy[tag] = false;
                try {
                    let [, out] = p.communicate_utf8_finish(res);
                    let fresh = JSON.parse(out);
                    if (only) this.data[only] = fresh[only];
                    else this.data = Object.assign(this.data || {}, fresh);
                } catch (e) {
                    global.logError("usage-meter: " + e);
                    if (!only) this.data = null;
                }
                this.loaded = true;
                this._render();
            });
        } catch (e) {
            this.busy[tag] = false;
            global.logError("usage-meter: " + e);
        }
    }

    _render() {
        if (!this.data) {
            this._setPanel([lbl("AI usage: error", "color: #ef5350;")]);
            this.set_applet_tooltip("Fetcher failed - see ~/.xsession-errors");
            return;
        }
        let kids = [], tips = [];
        let services = this._enabled();
        if (!services.length) {
            this._setPanel([lbl("AI usage", DIM)]);
            this.set_applet_tooltip("No subscriptions selected - click, then Settings");
            return;
        }
        services.forEach(([key, name], i) => {
            let s = this.data[key] || {};
            if (i > 0) kids.push(lbl(" │ ", "color: rgba(200,200,200,0.4);"));
            kids.push(this._logo(key, Math.min(this.getPanelIconSize(St.IconType.FULLCOLOR), 18)) ||
                lbl(name, "font-weight: bold;"));
            if (s.sessions) {
                // API-billed: no limit to colour against, so show the live session cost in the low-usage green.
                if (s.sessions.length) {
                    let usd = s.sessions.reduce((t, x) => t + (x.usd || 0), 0);
                    kids.push(lbl(money(usd), `color: ${colorFor(0)}; font-weight: bold;` + (s.stale ? " opacity: 0.55;" : "")));
                    if (s.sessions.length > 1) kids.push(lbl(`×${s.sessions.length}`, DIM));
                } else {
                    kids.push(lbl("idle", DIM));
                }
            } else if (!s.five_hour) {
                kids.push(lbl("—", DIM));
            } else {
                WINDOWS.forEach(([w, short], j) => {
                    if (j > 0) kids.push(lbl("·", DIM));
                    let pct = s[w].pct;
                    kids.push(lbl(short, DIM));
                    kids.push(lbl(Math.round(pct) + "%",
                        `color: ${colorFor(pct)}; font-weight: bold;` + (s.stale ? " opacity: 0.55;" : "")));
                });
            }
            if (s.error) kids.push(lbl("⚠", WARN));
            tips.push(`${name}: ` + (s.error || "ok"));
        });
        this._setPanel(kids);
        this.set_applet_tooltip(tips.join("\n") + "\nClick for details");
    }

    _addItem(actor) {
        let item = new PopupMenu.PopupBaseMenuItem({ reactive: false });
        item.addActor(actor);
        this.menu.addMenuItem(item);
    }

    _buildMenu() {
        this.menu.removeAll();
        let d = this.data || {};
        let services = this._enabled();
        let horizontal = this.menuLayout === "horizontal";
        if (!services.length) {
            this._addItem(lbl("No subscriptions selected", DIM));
        } else if (horizontal) {
            let cols = new St.BoxLayout({ style: "spacing: 28px;" });
            for (let [key, name] of services) cols.add_child(this._serviceColumn(key, name, d[key] || {}, true));
            this._addItem(cols);
        } else {
            services.forEach(([key, name], i) => {
                if (i > 0) this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
                this._addItem(this._serviceColumn(key, name, d[key] || {}, false));
            });
        }
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        if (d.updated) {
            this._addItem(lbl("Updated " + GLib.DateTime.new_from_unix_local(d.updated).format("%H:%M:%S"),
                DIM + " font-size: 0.85em;"));
        } else if (this.loaded) {
            this._addItem(lbl("Fetcher failed - see ~/.xsession-errors", WARN));
        }
        let refresh = new PopupMenu.PopupIconMenuItem("Refresh now", "view-refresh", St.IconType.SYMBOLIC);
        refresh.connect("activate", () => this._refresh());
        this.menu.addMenuItem(refresh);
        let settings = new PopupMenu.PopupIconMenuItem("Settings", "preferences-system", St.IconType.SYMBOLIC);
        settings.connect("activate", () => this.configureApplet());
        this.menu.addMenuItem(settings);
    }

    // One subscription's block; compact = narrower bars for the side-by-side layout.
    _serviceColumn(key, name, s, compact) {
        let col = new St.BoxLayout({ vertical: true, style: "spacing: 6px;" + (compact ? " max-width: 20em;" : "") });
        let head = [this._logo(key, 18), lbl(name, "font-weight: bold;")].filter(Boolean);
        if (s.plan) head.push(lbl(`(${s.plan})`, DIM));
        col.add_child(row(...head));
        // Antigravity splits its quota into model groups; the panel shows the Gemini one.
        let groups = s.groups || (s.five_hour ? [s] : []);
        for (let g of groups) {
            if (g.name) col.add_child(lbl(g.name, DIM + " font-size: 0.9em;"));
            for (let [w, , long] of WINDOWS) {
                let { pct, resets_at } = g[w];
                let c = colorFor(pct);
                let block = new St.BoxLayout({ vertical: true });
                block.add_child(row(bar(pct, c, compact ? 10 : 20),
                    lbl((pct > 0 && pct < 10 ? pct.toFixed(1) : Math.round(pct)) + "%",
                        `color: ${c}; font-weight: bold; min-width: 3em; text-align: right;`),
                    lbl(long)));
                block.add_child(lbl(untilText(resets_at), DIM + " font-size: 0.85em;"));
                col.add_child(block);
            }
        }
        if (s.sessions) this._addSessionRows(col, s.sessions);
        if (s.error) {
            let when = s.fetched ? ` - showing data from ${GLib.DateTime.new_from_unix_local(s.fetched).format("%H:%M")}` : "";
            col.add_child(wrapLbl("⚠ " + s.error + when, WARN + " font-size: 0.85em;"));
        }
        return col;
    }

    _addSessionRows(col, sessions) {
        if (!sessions.length) {
            col.add_child(lbl("No Muse session running", DIM));
            return;
        }
        for (let x of sessions) {
            let started = x.started ? " · started " + GLib.DateTime.new_from_unix_local(x.started).format("%H:%M") : "";
            let block = new St.BoxLayout({ vertical: true });
            block.add_child(row(lbl(GLib.path_get_basename(x.cwd), "min-width: 8em;"),
                lbl(x.usd === null ? "?" : money(x.usd), `color: ${colorFor(0)}; font-weight: bold;`)));
            block.add_child(lbl(`${x.calls} calls${started}`, DIM + " font-size: 0.85em;"));
            block.add_child(lbl(`${tokens(x.input)} input (${tokens(x.cached)} cached) · ${tokens(x.output)} output`,
                DIM + " font-size: 0.85em;"));
            col.add_child(block);
        }
        col.add_child(wrapLbl("Session cost, same totals as Muse's /usage", DIM + " font-size: 0.85em;"));
    }

    on_applet_clicked() {
        if (!this.menu.isOpen) this._buildMenu(); // keeps the reset countdowns current
        this.menu.toggle();
    }

    on_applet_removed_from_panel() {
        for (let t of this.timers) GLib.source_remove(t);
        this.timers = [];
    }
}

function main(metadata, orientation, panelHeight, instanceId) {
    return new UsageMeterApplet(metadata, orientation, panelHeight, instanceId);
}
