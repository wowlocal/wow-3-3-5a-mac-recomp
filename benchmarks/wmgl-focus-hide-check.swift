import AppKit
import CoreGraphics
import Darwin

func fail(_ message: String) -> Never {
    fputs(message + "\n", stderr)
    exit(1)
}
guard CommandLine.arguments.count == 3,
      let pid = pid_t(CommandLine.arguments[1]),
      let prior = pid_t(CommandLine.arguments[2]),
      pid != prior else { fail("Provide the trial PID and the app focused before launch.") }
var path = [CChar](repeating: 0, count: 4096)
guard proc_pidpath(pid, &path, UInt32(path.count)) > 0,
      String(cString: path).hasSuffix("/Wrath Trial.app/Contents/MacOS/ocerz"),
      let trial = NSRunningApplication(processIdentifier: pid),
      let previous = NSRunningApplication(processIdentifier: prior) else { fail("The requested app instances are missing.") }
let workspace = NSWorkspace.shared
func waitFor(_ condition: () -> Bool) -> Bool {
    let limit = Date().addingTimeInterval(3)
    while Date() < limit {
        if condition() { return true }
        RunLoop.current.run(until: Date().addingTimeInterval(0.07))
    }
    return condition()
}
func states() -> [[String: Any]] {
    let rows = CGWindowListCopyWindowInfo([.optionOnScreenOnly], kCGNullWindowID) as? [[String: Any]] ?? []
    return rows.filter { $0[kCGWindowOwnerPID as String] as? Int32 == pid }
}
func aligned() -> Bool {
    let rows = states()
    let named = rows.compactMap { row -> (String, CGRect)? in
        guard let name = row[kCGWindowName as String] as? String,
              let raw = row[kCGWindowBounds as String] as? NSDictionary,
              let rect = CGRect(dictionaryRepresentation: raw as CFDictionary) else { return nil }
        return (name,rect)
    }
    guard let backing = named.first(where: { $0.0 == "World of Warcraft" })?.1,
          let image = named.first(where: { $0.0 == "WoW Metal Trial Output" })?.1 else { return false }
    return image.width == 1365 && image.height == 768 && backing.width == image.width &&
           backing.minX == image.minX && backing.maxY == image.maxY
}
func returnToDesktop() {
    _ = trial.hide()
}
guard trial.isActive || trial.activate(options: [.activateAllWindows]) else { returnToDesktop(); fail("Could not activate the trial.") }
guard waitFor({ workspace.frontmostApplication?.processIdentifier == pid && aligned() }) else {
    returnToDesktop(); fail("Trial failed to show its aligned windows.")
}
print("initial: Carbon title bar and 1365x768 Metal image aligned")
for step in 1...2 {
    _ = previous.activate(options: [.activateAllWindows])
    guard waitFor({workspace.frontmostApplication?.processIdentifier == prior}) else {
        returnToDesktop(); fail("Could not return focus to the previous app.")
    }
    guard waitFor({trial.isHidden && states().isEmpty}) else {
        print("background: hidden \(trial.isHidden) visible windows \(states().count)")
        returnToDesktop(); fail("Trial did not hide itself after switching apps.")
    }
    print("switch \(step): previous app in foreground, visible WoW windows \(states().count)")
    if step == 1 {
        _ = trial.activate(options: [.activateAllWindows])
        guard waitFor({workspace.frontmostApplication?.processIdentifier == pid && !trial.isHidden && aligned()}) else {
            returnToDesktop(); fail("Trial failed to restore its aligned windows.")
        }
        print("return: aligned WoW windows restored")
    }
}
