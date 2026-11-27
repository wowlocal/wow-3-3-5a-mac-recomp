import AppKit
import CoreGraphics
import Foundation

let session = CGSessionCopyCurrentDictionary() as? [String: Any]
guard session?["CGSSessionScreenIsLocked"] as? Bool != true else {
    fputs("The desktop session is locked.\n", stderr)
    exit(1)
}
let workspace = NSWorkspace.shared
let identifier = "org.local.wrath-trial.headless"
let cutoff = Date().addingTimeInterval(22)
var seen = Set<pid_t>()
var activationCount = 0
var maximumWindowCount = 0
let watch = workspace.notificationCenter.addObserver(forName: NSWorkspace.didActivateApplicationNotification,
                                                      object: nil, queue: .main) { note in
    if let app = note.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication,
       app.bundleIdentifier == identifier {
        activationCount += 1
    }
}
defer { workspace.notificationCenter.removeObserver(watch) }
print("Watching the headless trial")
fflush(stdout)

while Date() < cutoff {
    for app in NSRunningApplication.runningApplications(withBundleIdentifier: identifier) {
        seen.insert(app.processIdentifier)
        let windows = CGWindowListCopyWindowInfo(.optionOnScreenOnly, kCGNullWindowID) as? [[String: Any]] ?? []
        let count = windows.filter { ($0[kCGWindowOwnerPID as String] as? Int) == Int(app.processIdentifier) }.count
        maximumWindowCount = max(maximumWindowCount, count)
        if count > 0 || activationCount > 0 || workspace.frontmostApplication?.processIdentifier == app.processIdentifier {
            // The monitor targets the isolated trial instance only.
            app.hide()
            let result = ["trial": Int(app.processIdentifier), "foregroundViolation": 1, "visibleWindows": count]
            print(String(data: try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys]), encoding: .utf8)!)
            exit(1)
        }
    }
    RunLoop.current.run(until: Date().addingTimeInterval(0.05))
}
let result = ["seen": Array(seen).sorted(), "activationNotifications": activationCount,
              "maxVisibleWindows": maximumWindowCount] as [String: Any]
print(String(data: try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys]), encoding: .utf8)!)
exit(seen.isEmpty ? 1 : 0)
