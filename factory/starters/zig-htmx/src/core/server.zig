//! Minimal HTTP Server and Router engine for HTMX applications.
//!
//! Handles connections via std.http.Server and standard net.Stream buffers.

const std = @import("std");
const net = std.net;

pub const Request = struct {
    target: []const u8,
    method: std.http.Method,
    is_htmx: bool,
    /// Request body, already read into the per-connection arena. Empty for GET/HEAD.
    body: []const u8 = "",
    /// Values captured from `{name}` segments of the matched route pattern.
    params: []const Param = &.{},

    pub const Param = struct { name: []const u8, value: []const u8 };

    pub fn param(self: Request, name: []const u8) ?[]const u8 {
        for (self.params) |p| {
            if (std.mem.eql(u8, p.name, name)) return p.value;
        }
        return null;
    }

    /// A form field from an `application/x-www-form-urlencoded` body, URL-decoded.
    pub fn form(self: Request, arena: std.mem.Allocator, name: []const u8) !?[]const u8 {
        var it = std.mem.splitScalar(u8, self.body, '&');
        while (it.next()) |pair| {
            const eq = std.mem.indexOfScalar(u8, pair, '=') orelse continue;
            if (!std.mem.eql(u8, pair[0..eq], name)) continue;
            return try urlDecode(arena, pair[eq + 1 ..]);
        }
        return null;
    }
};

/// Percent-decode and turn '+' into a space.
pub fn urlDecode(arena: std.mem.Allocator, input: []const u8) ![]const u8 {
    var out = std.ArrayList(u8).empty;
    var i: usize = 0;
    while (i < input.len) {
        switch (input[i]) {
            '+' => {
                try out.append(arena, ' ');
                i += 1;
            },
            '%' => {
                if (i + 2 >= input.len) {
                    try out.append(arena, input[i]);
                    i += 1;
                    continue;
                }
                const byte = std.fmt.parseInt(u8, input[i + 1 .. i + 3], 16) catch {
                    try out.append(arena, input[i]);
                    i += 1;
                    continue;
                };
                try out.append(arena, byte);
                i += 3;
            },
            else => {
                try out.append(arena, input[i]);
                i += 1;
            },
        }
    }
    return out.toOwnedSlice(arena);
}

/// Escape text for HTML content AND for a double-quoted attribute value.
///
/// Both positions matter: a product name containing `<img>` broke out of a table cell, and one
/// containing a double quote broke out of `data-nama="..."`. Measured on a real build: the
/// reviewer rejected a route task for exactly this stored XSS, and the worker had no helper to
/// fix it, so the task failed three times and the build never succeeded.
pub fn escapeHtml(arena: std.mem.Allocator, input: []const u8) ![]const u8 {
    var out = std.ArrayList(u8).empty;
    for (input) |ch| {
        switch (ch) {
            '&' => try out.appendSlice(arena, "&amp;"),
            '<' => try out.appendSlice(arena, "&lt;"),
            '>' => try out.appendSlice(arena, "&gt;"),
            '"' => try out.appendSlice(arena, "&quot;"),
            '\'' => try out.appendSlice(arena, "&#39;"),
            else => try out.append(arena, ch),
        }
    }
    return out.toOwnedSlice(arena);
}

/// Replace the first occurrence of `{{KEY}}` in `template`.
///
/// The chassis layout carries `{{CONTENT}}`; before this the index route returned the layout
/// verbatim, so the served page contained the literal text `{{CONTENT}}` and the application
/// looked empty in a browser while `/api/health` answered 200.
pub fn fillSlot(arena: std.mem.Allocator, template: []const u8, key: []const u8,
                value: []const u8) ![]const u8 {
    const marker = try std.fmt.allocPrint(arena, "{{{{{s}}}}}", .{key});
    const at = std.mem.indexOf(u8, template, marker) orelse return template;
    return std.fmt.allocPrint(arena, "{s}{s}{s}", .{
        template[0..at], value, template[at + marker.len ..],
    });
}

/// Replace every `{{KEY}}` with `value`.
pub fn fillAll(arena: std.mem.Allocator, template: []const u8, key: []const u8,
               value: []const u8) ![]const u8 {
    const marker = try std.fmt.allocPrint(arena, "{{{{{s}}}}}", .{key});
    var out = std.ArrayList(u8).empty;
    var rest = template;
    while (std.mem.indexOf(u8, rest, marker)) |at| {
        try out.appendSlice(arena, rest[0..at]);
        try out.appendSlice(arena, value);
        rest = rest[at + marker.len ..];
    }
    try out.appendSlice(arena, rest);
    return out.toOwnedSlice(arena);
}

pub const Response = struct {
    raw_request: *std.http.Server.Request,

    pub fn html(self: *Response, body: []const u8) !void {
        try self.raw_request.respond(body, .{
            .status = .ok,
            .extra_headers = &.{.{ .name = "content-type", .value = "text/html; charset=utf-8" }},
        });
    }

    pub fn json(self: *Response, body: []const u8) !void {
        try self.raw_request.respond(body, .{
            .status = .ok,
            .extra_headers = &.{.{ .name = "content-type", .value = "application/json" }},
        });
    }

    /// An error with a real status code. `notFound`/`errorResponse` cover the common cases; this
    /// covers the rest (413 for an oversized body) without inventing a helper per code.
    pub fn errorStatus(self: *Response, status: u16, message: []const u8) !void {
        const real: std.http.Status = if (status >= 100 and status <= 599)
            @enumFromInt(status)
        else
            .internal_server_error;
        try self.raw_request.respond(message, .{
            .status = real,
            .extra_headers = &.{.{ .name = "content-type", .value = "text/plain; charset=utf-8" }},
        });
    }

    pub fn notFound(self: *Response) !void {
        try self.raw_request.respond("Not found", .{ .status = .not_found });
    }

    pub fn errorResponse(self: *Response, message: []const u8) !void {
        try self.raw_request.respond(message, .{ .status = .internal_server_error });
    }
};

pub const Handler = *const fn (req: *Request, res: *Response) anyerror!void;

pub const Route = struct {
    method: std.http.Method,
    path: []const u8,
    handler: Handler,
};

pub const Router = struct {
    routes: std.ArrayList(Route),
    allocator: std.mem.Allocator,

    pub fn init(allocator: std.mem.Allocator) Router {
        return Router{
            .routes = .empty,
            .allocator = allocator,
        };
    }

    pub fn deinit(self: *Router) void {
        self.routes.deinit(self.allocator);
    }

    pub fn get(self: *Router, path: []const u8, handler: Handler) !void {
        try self.route(.GET, path, handler);
    }

    pub fn post(self: *Router, path: []const u8, handler: Handler) !void {
        try self.route(.POST, path, handler);
    }

    pub fn put(self: *Router, path: []const u8, handler: Handler) !void {
        try self.route(.PUT, path, handler);
    }

    pub fn patch(self: *Router, path: []const u8, handler: Handler) !void {
        try self.route(.PATCH, path, handler);
    }

    pub fn delete(self: *Router, path: []const u8, handler: Handler) !void {
        try self.route(.DELETE, path, handler);
    }

    pub fn route(self: *Router, method: std.http.Method, path: []const u8, handler: Handler) !void {
        try self.routes.append(self.allocator, .{
            .method = method,
            .path = path,
            .handler = handler,
        });
    }

    pub fn dispatch(self: *Router, arena: std.mem.Allocator, req: *Request, res: *Response) !void {
        const path = pathOnly(req.target);
        // An exact match wins first, so a literal path always beats a wildcard pattern.
        for (self.routes.items) |entry| {
            if (entry.method == req.method and std.mem.eql(u8, entry.path, path)) {
                try entry.handler(req, res);
                return;
            }
        }
        // Then a pattern match that captures `{name}` or `:name` segments.
        //
        // Without this a route like `/produk/{id}/jual` cannot be expressed, and a worker facing
        // that gap registered 65,536 literal routes in a loop -- tens of thousands of heap
        // allocations at boot and an unsynchronised router mutated from a request handler. The
        // reviewer rejected it, correctly, and the build failed after three attempts.
        for (self.routes.items) |entry| {
            if (entry.method != req.method) continue;
            var params = std.ArrayList(Request.Param).empty;
            if (try matchPatternPublic(arena, entry.path, path, &params)) {
                req.params = try params.toOwnedSlice(arena);
                try entry.handler(req, res);
                return;
            }
        }
        try res.notFound();
    }

    fn pathOnly(target: []const u8) []const u8 {
        const end = std.mem.indexOfAny(u8, target, "?#") orelse target.len;
        return target[0..end];
    }

    /// Match `pattern` (with `{name}` or `:name` segments) against `target`, appending captured values.
    pub fn matchPatternPublic(arena: std.mem.Allocator, pattern: []const u8, target: []const u8,
                    params: *std.ArrayList(Request.Param)) !bool {
        var p = std.mem.splitScalar(u8, pattern, '/');
        var t = std.mem.splitScalar(u8, target, '/');
        while (true) {
            const p_seg = p.next();
            const t_seg = t.next();
            if (p_seg == null and t_seg == null) return true;
            if (p_seg == null or t_seg == null) return false;
            const ps = p_seg.?;
            const ts = t_seg.?;
            if (ps.len >= 2 and ps[0] == '{' and ps[ps.len - 1] == '}') {
                if (ts.len == 0) return false;
                try params.append(arena, .{
                    .name = ps[1 .. ps.len - 1],
                    .value = try arena.dupe(u8, ts),
                });
                continue;
            }
            if (ps.len >= 2 and ps[0] == ':') {
                if (ts.len == 0) return false;
                try params.append(arena, .{
                    .name = ps[1..],
                    .value = try arena.dupe(u8, ts),
                });
                continue;
            }
            if (!std.mem.eql(u8, ps, ts)) return false;
        }
    }
};

pub const Server = struct {
    port: u16,
    router: *Router,
    allocator: std.mem.Allocator,

    pub fn init(allocator: std.mem.Allocator, port: u16, router: *Router) Server {
        return Server{
            .port = port,
            .router = router,
            .allocator = allocator,
        };
    }

    pub fn listen(self: *Server) !void {
        const addr = try net.Address.parseIp("127.0.0.1", self.port);
        var listener = try addr.listen(.{ .reuse_address = true });
        defer listener.deinit();

        while (true) {
            const conn = listener.accept() catch continue;
            self.handleConn(conn) catch {};
            conn.stream.close();
        }
    }

    fn handleConn(self: *Server, conn: net.Server.Connection) !void {
        var read_buf: [8192]u8 = undefined;
        var write_buf: [8192]u8 = undefined;
        var stream_reader = conn.stream.reader(&read_buf);
        var stream_writer = conn.stream.writer(&write_buf);
        var http_server = std.http.Server.init(stream_reader.interface(), &stream_writer.interface);

        var raw_req = http_server.receiveHead() catch return;
        const path = raw_req.head.target;

        var is_htmx = false;
        var it = raw_req.iterateHeaders();
        while (it.next()) |header| {
            if (std.ascii.eqlIgnoreCase(header.name, "hx-request")) {
                if (std.mem.eql(u8, header.value, "true")) {
                    is_htmx = true;
                }
            }
        }

        // Everything the handler may keep must be copied: `path` and the header values point
        // into the connection buffer and are invalidated once the body is read.
        var arena_state = std.heap.ArenaAllocator.init(self.allocator);
        defer arena_state.deinit();
        const arena = arena_state.allocator();
        const owned_target = try arena.dupe(u8, path);

        const body = readBody(&raw_req, arena) catch |err| switch (err) {
            error.BodyTooLarge => {
                var too_large = Response{ .raw_request = &raw_req };
                try too_large.errorStatus(413, "Payload too large");
                try stream_writer.interface.flush();
                return;
            },
            else => return err,
        };

        var req = Request{
            .target = owned_target,
            .method = raw_req.head.method,
            .is_htmx = is_htmx,
            .body = body,
        };
        var res = Response{
            .raw_request = &raw_req,
        };

        try self.router.dispatch(arena, &req, &res);
        try stream_writer.interface.flush();
    }

    /// Read a POST body, capped so an unbounded upload cannot exhaust memory.
    ///
    /// The limit is applied by the reader as well as by `content_length`, because a chunked
    /// request carries no length header and would otherwise pass an unbounded body through.
    fn readBody(raw_req: *std.http.Server.Request, arena: std.mem.Allocator) ![]const u8 {
        const tag = @tagName(raw_req.head.method);
        if (std.mem.eql(u8, tag, "GET") or std.mem.eql(u8, tag, "HEAD")) return "";
        if (raw_req.head.content_length) |len| {
            if (len > MAX_BODY) return error.BodyTooLarge;
        }
        var buf: [8192]u8 = undefined;
        const reader = raw_req.readerExpectNone(&buf);
        return reader.allocRemaining(arena, std.Io.Limit.limited(MAX_BODY)) catch |err| switch (err) {
            error.StreamTooLong, error.OutOfMemory => error.BodyTooLarge,
            else => err,
        };
    }
};

/// 1 MiB. A form post is a few hundred bytes; anything near this is a mistake or an attack.
pub const MAX_BODY: usize = 1 << 20;

// ---------------------------------------------------------------------------------------
// Regression tests for the defects that made a real build fail or serve a blank page.
// ---------------------------------------------------------------------------------------

test "escapeHtml neutralises a script tag in content position" {
    const a = std.testing.allocator;
    var arena_state = std.heap.ArenaAllocator.init(a);
    defer arena_state.deinit();
    const out = try escapeHtml(arena_state.allocator(), "<img src=x onerror=alert(1)>");
    try std.testing.expect(std.mem.indexOf(u8, out, "<img") == null);
}

test "escapeHtml neutralises a double quote in attribute position" {
    const a = std.testing.allocator;
    var arena_state = std.heap.ArenaAllocator.init(a);
    defer arena_state.deinit();
    const out = try escapeHtml(arena_state.allocator(), "x\" onmouseover=\"alert(1)");
    try std.testing.expect(std.mem.indexOf(u8, out, "\"") == null);
}

test "escapeHtml leaves ordinary text alone" {
    const a = std.testing.allocator;
    var arena_state = std.heap.ArenaAllocator.init(a);
    defer arena_state.deinit();
    const out = try escapeHtml(arena_state.allocator(), "Kopi Susu 1kg");
    try std.testing.expectEqualStrings("Kopi Susu 1kg", out);
}

test "fillSlot replaces the CONTENT marker" {
    // Measured: the index route returned the layout verbatim, so the served page contained the
    // literal `{{CONTENT}}` and the browser showed nothing while /api/health answered 200.
    const a = std.testing.allocator;
    var arena_state = std.heap.ArenaAllocator.init(a);
    defer arena_state.deinit();
    const out = try fillSlot(arena_state.allocator(), "<main>{{CONTENT}}</main>", "CONTENT", "<p>hi</p>");
    try std.testing.expectEqualStrings("<main><p>hi</p></main>", out);
}

test "fillSlot returns the template unchanged when the marker is absent" {
    const a = std.testing.allocator;
    var arena_state = std.heap.ArenaAllocator.init(a);
    defer arena_state.deinit();
    const out = try fillSlot(arena_state.allocator(), "<p>plain</p>", "CONTENT", "x");
    try std.testing.expectEqualStrings("<p>plain</p>", out);
}

test "fillAll replaces every occurrence" {
    const a = std.testing.allocator;
    var arena_state = std.heap.ArenaAllocator.init(a);
    defer arena_state.deinit();
    const out = try fillAll(arena_state.allocator(), "{{R}}-{{R}}-{{R}}", "R", "x");
    try std.testing.expectEqualStrings("x-x-x", out);
}

test "urlDecode handles plus and percent escapes" {
    const a = std.testing.allocator;
    var arena_state = std.heap.ArenaAllocator.init(a);
    defer arena_state.deinit();
    const arena = arena_state.allocator();
    try std.testing.expectEqualStrings("Kopi Susu", try urlDecode(arena, "Kopi+Susu"));
    try std.testing.expectEqualStrings("a b", try urlDecode(arena, "a%20b"));
    try std.testing.expectEqualStrings("100%", try urlDecode(arena, "100%25"));
}

test "a route pattern captures a path parameter" {
    // Without this a route like /produk/{id}/jual cannot be expressed, and a worker registered
    // 65,536 literal routes instead -- rejected by the reviewer, build failed three times.
    const a = std.testing.allocator;
    var arena_state = std.heap.ArenaAllocator.init(a);
    defer arena_state.deinit();
    const arena = arena_state.allocator();
    var params = std.ArrayList(Request.Param).empty;
    const matched = try Router.matchPatternPublic(arena, "/produk/{id}/jual", "/produk/42/jual", &params);
    try std.testing.expect(matched);
    try std.testing.expectEqual(@as(usize, 1), params.items.len);
    try std.testing.expectEqualStrings("id", params.items[0].name);
    try std.testing.expectEqualStrings("42", params.items[0].value);
}

test "a route pattern rejects a different shape" {
    const a = std.testing.allocator;
    var arena_state = std.heap.ArenaAllocator.init(a);
    defer arena_state.deinit();
    const arena = arena_state.allocator();
    var params = std.ArrayList(Request.Param).empty;
    try std.testing.expect(!try Router.matchPatternPublic(arena, "/produk/{id}/jual", "/produk/42", &params));
    try std.testing.expect(!try Router.matchPatternPublic(arena, "/produk/{id}/jual", "/lain/42/jual", &params));
}
