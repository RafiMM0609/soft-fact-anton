const std = @import("std");

pub fn build(b: *std.Build) void {
    const target = b.standardTargetOptions(.{});
    const optimize = b.standardOptimizeOption(.{});

    const mod = b.createModule(.{
        .root_source_file = b.path("src/main.zig"),
        .target = target,
        .optimize = optimize,
    });

    mod.addIncludePath(b.path("src/c"));
    mod.link_libc = true;

    const exe = b.addExecutable(.{
        .name = "app",
        .root_module = mod,
    });

    // Link SQLite: use precompiled static library if present, or compile from C source, or link system library
    var has_precompiled = false;
    if (b.build_root.handle.access("src/c/libsqlite3.a", .{})) {
        has_precompiled = true;
    } else |_| {}

    var has_source = false;
    if (b.build_root.handle.access("src/c/sqlite3.c", .{})) {
        has_source = true;
    } else |_| {}

    if (has_precompiled) {
        exe.addObjectFile(b.path("src/c/libsqlite3.a"));
    } else if (has_source) {
        mod.addCSourceFile(.{
            .file = b.path("src/c/sqlite3.c"),
            .flags = &.{"-std=c99"},
        });
    } else {
        exe.linkSystemLibrary("sqlite3");
    }

    b.installArtifact(exe);

    // Tests are discovered from `src/core/` and any `src/**/*_test.zig`, so a feature task adds a
    // file and never edits this script. A fixed list forced every feature to edit a shared file,
    // which two parallel tasks cannot do without a merge conflict.
    const test_step = b.step("test", "Run the chassis and feature tests");
    var directories = std.ArrayList([]const u8).empty;
    defer directories.deinit(b.allocator);
    directories.append(b.allocator, "src/core") catch @panic("out of memory");
    if (std.fs.cwd().openDir("src", .{ .iterate = true })) |opened| {
        var dir = opened;
        defer dir.close();
        var it = dir.iterate();
        while (it.next() catch null) |entry| {
            if (entry.kind == .directory) {
                directories.append(b.allocator, b.pathJoin(&.{ "src", entry.name })) catch
                    @panic("out of memory");
            }
        }
    } else |_| {}

    for (directories.items) |directory| {
        const dir = std.fs.cwd().openDir(directory, .{ .iterate = true }) catch continue;
        var dir_mut = dir;
        defer dir_mut.close();
        var it = dir_mut.iterate();
        while (it.next() catch null) |entry| {
            if (entry.kind != .file) continue;
            if (!std.mem.endsWith(u8, entry.name, ".zig")) continue;
            const is_test_file = std.mem.endsWith(u8, entry.name, "_test.zig");
            const is_core = std.mem.startsWith(u8, directory, "src/core");
            if (!is_test_file and !is_core) continue;
            const tests = b.addTest(.{ .root_module = b.createModule(.{
                .root_source_file = b.path(b.pathJoin(&.{ directory, entry.name })),
                .target = target,
                .optimize = optimize,
            }) });
            tests.root_module.addIncludePath(b.path("src/c"));
            tests.root_module.link_libc = true;
            tests.root_module.addObjectFile(b.path("src/c/libsqlite3.a"));
            test_step.dependOn(&b.addRunArtifact(tests).step);
        }
    }
}
