//! Application Main Entrypoint.
//!
//! Boots the SQLite database, registers routes, and starts the HTTP server.

const std = @import("std");

/// The schema is embedded here, at the package root, because `@embedFile` may not reach outside
/// the package. `db.zig` is also compiled on its own by `zig build test`, so it cannot own this.
const SCHEMA = @embedFile("schema.sql");
const server = @import("core/server.zig");
const db = @import("core/db.zig");
const routes = @import("routes.zig");

pub fn main() !void {
    var gpa = std.heap.GeneralPurposeAllocator(.{}){};
    defer _ = gpa.deinit();
    const allocator = gpa.allocator();

    // 1. Initialize SQLite Database (reads DATABASE_URL or DATA_DIR)
    var database = try db.DB.openFromEnv(allocator);
    defer database.close();

    // 2. Initialize Router and register routes
    var router = server.Router.init(allocator);
    defer router.deinit();

    try database.applySchema(SCHEMA);
    try routes.initRoutes(&router, &database);

    // 3. Resolve PORT (default 3000)
    const port: u16 = blk: {
        const raw = std.process.getEnvVarOwned(allocator, "PORT") catch break :blk 3000;
        defer allocator.free(raw);
        break :blk std.fmt.parseInt(u16, raw, 10) catch 3000;
    };

    // 4. Start HTTP Server
    var app_server = server.Server.init(allocator, port, &router);
    try app_server.listen();
}
