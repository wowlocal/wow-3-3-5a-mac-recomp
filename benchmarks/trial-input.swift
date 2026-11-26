import AppKit
import CoreGraphics
import Darwin

func fail(_ reason: String) -> Never {
    FileHandle.standardError.write(Data((reason + "\n").utf8))
    exit(1)
}

let arguments = CommandLine.arguments
guard [2, 4, 6, 8].contains(arguments.count),
      let pid = pid_t(arguments[1]) else {
    fail("Supply the PID of the running Wrath Trial, then optional normalized x and y.")
}
var executableBuffer = [CChar](repeating: 0, count: 4096)
let length = proc_pidpath(pid, &executableBuffer, UInt32(executableBuffer.count))
guard length > 0, String(cString: executableBuffer).hasSuffix("/Wrath Trial.app/Contents/MacOS/ocerz") else {
    fail("The PID does not belong to the Wrath Trial runtime.")
}

let info = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID)
guard let windows = info as? [[String: Any]],
      let bounds = windows.compactMap({ window -> CGRect? in
          guard window[kCGWindowOwnerPID as String] as? pid_t == pid,
                window[kCGWindowLayer as String] as? Int == 0,
                let raw = window[kCGWindowBounds as String] as? NSDictionary,
                let frame = CGRect(dictionaryRepresentation: raw as CFDictionary),
                frame.width > 500, frame.height > 350 else { return nil }
          return frame
      }).first else {
    fail("The trial process has no visible game-sized window.")
}

let canPost = CGPreflightPostEventAccess()
print("Trial window \(Int(bounds.width))×\(Int(bounds.height)) at (\(Int(bounds.minX)), \(Int(bounds.minY))). Input access \(canPost).")
guard arguments.count > 2 else { exit(0) }
guard canPost else { fail("macOS has not granted this process permission to post events.") }
guard let x = Double(arguments[2]), let y = Double(arguments[3]),
      (0.02 ... 0.98).contains(x), (0.06 ... 0.98).contains(y) else {
    fail("Coordinates must be fractions inside the game's content area.")
}

let button = arguments.count >= 6 ? arguments[4] : "left"
let holdMs = arguments.count >= 6 ? Int(arguments[5]) : 100
guard button == "left" || button == "right", let holdMs, (30 ... 3000).contains(holdMs) else {
    fail("Choose left or right and a hold from 30 to 3000 ms.")
}
let point = CGPoint(x: bounds.minX + bounds.width * x, y: bounds.minY + bounds.height * y)
var endPoint = point
if arguments.count == 8 {
    guard let endX = Double(arguments[6]), let endY = Double(arguments[7]),
          (0.02 ... 0.98).contains(endX), (0.06 ... 0.98).contains(endY) else {
        fail("The drag must end inside the game's content area.")
    }
    endPoint = CGPoint(x: bounds.minX + bounds.width * endX, y: bounds.minY + bounds.height * endY)
}
guard let source = CGEventSource(stateID: .hidSystemState) else { fail("Could not initialize mouse events.") }
let right = button == "right"
let mouseButton: CGMouseButton = right ? .right : .left
let down: CGEventType = right ? .rightMouseDown : .leftMouseDown
let up: CGEventType = right ? .rightMouseUp : .leftMouseUp
let dragged: CGEventType = right ? .rightMouseDragged : .leftMouseDragged

func post(_ type: CGEventType, at location: CGPoint, dx: Int64 = 0, dy: Int64 = 0) {
    guard let event = CGEvent(mouseEventSource: source, mouseType: type, mouseCursorPosition: location, mouseButton: mouseButton) else {
        fail("Could not construct the mouse event.")
    }
    event.setIntegerValueField(.mouseEventClickState, value: 1)
    event.setIntegerValueField(.mouseEventDeltaX, value: dx)
    event.setIntegerValueField(.mouseEventDeltaY, value: dy)
    event.postToPid(pid)
}

post(.mouseMoved, at: point)
usleep(33_000)
let start = clock_gettime_nsec_np(CLOCK_MONOTONIC)
post(down, at: point)
if arguments.count == 8 {
    let steps = max(1, holdMs / 30)
    var previous = point
    for step in 1 ... steps {
        usleep(useconds_t(holdMs * 1_000 / steps))
        let fraction = CGFloat(step) / CGFloat(steps)
        let location = CGPoint(x: point.x + fraction * (endPoint.x - point.x),
                               y: point.y + fraction * (endPoint.y - point.y))
        post(dragged, at: location, dx: Int64(location.x.rounded() - previous.x.rounded()),
             dy: Int64(location.y.rounded() - previous.y.rounded()))
        previous = location
    }
} else {
    usleep(useconds_t(holdMs * 1_000))
}
post(up, at: endPoint)
let stop = clock_gettime_nsec_np(CLOCK_MONOTONIC)
print("Hold \(button) \(start) \(stop)")
