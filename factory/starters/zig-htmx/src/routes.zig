//! Application Routes.
//!
//! Workers register new domain endpoints and HTMX view handlers here.

const std = @import("std");
const server = @import("core/server.zig");
const db = @import("core/db.zig");

const LAYOUT = @embedFile("views/layout.html");
const HTMX_JS = @embedFile("views/htmx.min.js");

var global_db: ?*db.DB = null;

pub fn initRoutes(router: *server.Router, database: *db.DB) !void {
    global_db = database;

    try router.get("/api/health", handleHealth);
    try router.get("/assets/htmx.min.js", handleHtmxAsset);
    try router.get("/", handleIndex);
    try router.get("/api/items", handleListItems);
}

fn handleHealth(req: *server.Request, res: *server.Response) !void {
    _ = req;
    try res.json("{\"status\":\"ok\"}");
}

/// Serve vendored htmx build from the same origin to satisfy isolated testing environments.
fn handleHtmxAsset(req: *server.Request, res: *server.Response) !void {
    _ = req;
    try res.raw_request.respond(HTMX_JS, .{
        .status = .ok,
        .extra_headers = &.{
            .{ .name = "content-type", .value = "application/javascript; charset=utf-8" },
            .{ .name = "cache-control", .value = "public, max-age=3600" },
        },
    });
}

fn handleIndex(req: *server.Request, res: *server.Response) !void {
    _ = req;
    // The layout carries a `{{CONTENT}}` slot; returning LAYOUT verbatim served the literal
    // marker, so the browser showed an empty page while `/api/health` answered 200. Measured on
    // a real build: the whole application looked blank at port 8321 for this reason.
    var arena_state = std.heap.ArenaAllocator.init(std.heap.page_allocator);
    defer arena_state.deinit();
    const arena = arena_state.allocator();
    const page = try server.fillAll(arena, LAYOUT, "CONTENT", try content(arena));
    try res.html(page);
}

/// The default page body. A feature task replaces this with its own fragment.
fn content(arena: std.mem.Allocator) ![]const u8 {
    return std.fmt.allocPrint(arena,
        \\<section class="card">
        \\  <h1>Sasis siap dipakai</h1>
        \\  <p>Server, SQLite, escaping dan routing sudah tersedia. Ganti bagian ini
        \\  dengan fitur Anda: isi slot <code>CONTENT</code> di
        \\  <code>src/views/layout.html</code> dengan fragmen dari handler Anda.</p>
        \\  <p>Query lewat <code>src/core/db.zig</code>: <code>db.query</code> dengan
        \\  parameter terikat, jangan memanggil <code>sqlite3.h</code> langsung.</p>
        \\</section>
    , .{});
}

fn handleListItems(req: *server.Request, res: *server.Response) !void {
    _ = req;
    const fragment =
        \\<div class="card">
        \\  <h3>Daftar Barang (Master Data)</h3>
        \\  <table>
        \\    <thead>
        \\      <tr><th>ID</th><th>Nama</th><th>SKU</th><th>Stok</th></tr>
        \\    </thead>
        \\    <tbody>
        \\      <tr><td>1</td><td>Contoh Item A</td><td>SKU-001</td><td>100</td></tr>
        \\    </tbody>
        \\  </table>
        \\</div>
    ;
    try res.html(fragment);
}
