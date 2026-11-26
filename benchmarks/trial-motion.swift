import AppKit
import Carbon
import CoreGraphics
import Darwin

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(1)
}

let arguments = CommandLine.arguments
guard arguments.count == 2 || arguments.count == 4, let pid = pid_t(arguments[1]) else {
    fail("Usage: trial-motion PID [w|s|q|e|a|d|up|escape MILLISECONDS]")
}
var pathBytes = [CChar](repeating: 0, count: 4096)
guard proc_pidpath(pid, &pathBytes, UInt32(pathBytes.count)) > 0,
      String(cString: pathBytes).hasSuffix("/Wrath Trial.app/Contents/MacOS/ocerz"),
      NSWorkspace.shared.frontmostApplication?.processIdentifier == pid,
      CGPreflightPostEventAccess() else {
    fail("The foreground process must be the accessible Wrath Trial client.")
}
guard let session = CGSessionCopyCurrentDictionary() as? [String: Any],
      session["CGSSessionScreenIsLocked"] as? Bool != true else {
    fail("The desktop must be unlocked.")
}
if arguments.count == 2 {
    print("Wrath Trial is in the foreground.")
    exit(0)
}

let codes: [String: CGKeyCode] = [
    "w": CGKeyCode(kVK_ANSI_W), "s": CGKeyCode(kVK_ANSI_S),
    "q": CGKeyCode(kVK_ANSI_Q), "e": CGKeyCode(kVK_ANSI_E),
    "a": CGKeyCode(kVK_ANSI_A), "d": CGKeyCode(kVK_ANSI_D),
    "up": CGKeyCode(kVK_UpArrow), "escape": CGKeyCode(kVK_Escape),
]
guard let code = codes[arguments[2]], let duration = Int(arguments[3]),
      (30...2000).contains(duration) else {
    fail("Choose a route key and a hold between 30 and 2000 milliseconds.")
}
guard let source = CGEventSource(stateID: .hidSystemState),
      let down = CGEvent(keyboardEventSource: source, virtualKey: code, keyDown: true),
      let up = CGEvent(keyboardEventSource: source, virtualKey: code, keyDown: false) else {
    fail("Unable to construct a key press.")
}
down.flags = []
up.flags = []
let start = clock_gettime_nsec_np(CLOCK_MONOTONIC)
down.post(tap: .cghidEventTap)
usleep(useconds_t(duration * 1000))
up.post(tap: .cghidEventTap)
let stop = clock_gettime_nsec_np(CLOCK_MONOTONIC)
guard NSWorkspace.shared.frontmostApplication?.processIdentifier == pid else {
    fail("The game lost focus while the key was held.")
}
print("Key \(arguments[2]) \(start) \(stop)")
