// Проверка кросс-сборки: статический Hello World под aarch64 musl.
const std = @import("std");

pub fn main() !void {
    var buf: [64]u8 = undefined;
    const msg = try std.fmt.bufPrint(&buf, "hello aarch64 musl zig {s}\n", .{@import("builtin").zig_version_string});
    try std.io.getStdOut().writeAll(msg);
}
