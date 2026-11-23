//! Guards the starter contract: the chassis ships no sample tables.
//!
//! Measured: `schema.sql` defined `items` and `inventory_logs`. A worker asked for a `produk`
//! table replaced them, and the reviewer rejected the task for removing tables that were never
//! part of the brief -- a wasted attempt caused by the starter, not by the worker.

const std = @import("std");

test "the starter schema defines no tables the brief did not ask for" {
    const schema = @embedFile("schema.sql");
    try std.testing.expect(std.mem.indexOf(u8, schema, "CREATE TABLE") == null);
}
