// Adapted from UniiChat. Copyright 2026 Higher Order Company.
// Apache-2.0; see LICENSE-APACHE-2.0 and THIRD_PARTY_NOTICES.md.
// Changes: argument and permission checks, interrupted-action input release.
// Computer
// ========
// The mouse and keyboard of the computer tool (Computer.ts compiles and
// runs this), in the shape of Anthropic's computer use tool: it posts the
// events macOS gets from a real mouse and keyboard, which needs the
// Accessibility permission. Its one argument is {name, input} as JSON: an
// action (or press and release, the two halves of hold_key), with the
// action's input, whose coordinates are pixels of the screenshot. The
// screenshot is the screen in points, shrunk to fit BOX when larger (the
// size the APIs take whole, WXGA), so coordinates are multiplied back by
// that scale here. It prints {w, h, scale, text}: the screenshot's size,
// its scale, and what the action answers.

import ApplicationServices
import CoreGraphics
import Foundation

let BOX = (w: 1280.0, h: 800.0)

// the virtual key codes of a US keyboard, by lowercased name
let KEYS: [String: CGKeyCode] = [
  "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11, "q": 12,
  "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23,
  "=": 24, "9": 25, "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34,
  "p": 35, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44, "n": 45, "m": 46,
  ".": 47, "`": 50, "return": 36, "enter": 36, "tab": 48, "space": 49, "backspace": 51, "escape": 53,
  "esc": 53, "delete": 117, "home": 115, "end": 119, "page_up": 116, "pageup": 116, "prior": 116,
  "page_down": 121, "pagedown": 121, "next": 121, "left": 123, "right": 124, "down": 125, "up": 126,
  "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100, "f9": 101,
  "f10": 109, "f11": 103, "f12": 111, "shift": 56, "ctrl": 59, "control": 59, "alt": 58,
  "option": 58, "super": 55, "cmd": 55, "command": 55, "meta": 55, "minus": 27, "equal": 24,
  "comma": 43, "period": 47, "slash": 44, "semicolon": 41, "apostrophe": 39, "grave": 50,
  "backslash": 42, "bracketleft": 33, "bracketright": 30, "kp_enter": 76,
]

// the modifiers, by lowercased name
let MODS: [String: CGEventFlags] = [
  "shift": .maskShift, "ctrl": .maskControl, "control": .maskControl, "alt": .maskAlternate,
  "option": .maskAlternate, "super": .maskCommand, "cmd": .maskCommand, "command": .maskCommand,
  "meta": .maskCommand, "fn": .maskSecondaryFn,
]

func fail(_ msg: String) -> Never {
  FileHandle.standardError.write(Data((msg + "\n").utf8))
  exit(1)
}

guard CommandLine.arguments.count == 2 else { fail("Pass a JSON action") }
let arg = (try? JSONSerialization.jsonObject(with: Data(CommandLine.arguments[1].utf8))) as? [String: Any] ?? [:]
let name = arg["name"] as? String ?? ""
let input = arg["input"] as? [String: Any] ?? [:]
let screen = CGDisplayBounds(CGMainDisplayID())
let scale = max(1, screen.width / BOX.w, screen.height / BOX.h)
if name != "release" && name != "reset" {
  guard CGPreflightScreenCaptureAccess() else {
    fail("Enable Screen Recording for the app running EternalChat in System Settings > Privacy & Security, then restart it.")
  }
  if !["screenshot", "zoom", "wait", "cursor_position"].contains(name) && !AXIsProcessTrusted() {
    fail("Enable Accessibility for the app running EternalChat in System Settings > Privacy & Security, then restart it.")
  }
}

func text(_ key: String) -> String {
  guard let s = input[key] as? String else { fail("the input needs a string " + key) }
  return s
}

// a screenshot point of the input, in screen points; nil if absent
func point(_ key: String) -> CGPoint? {
  guard let xy = input[key] as? [NSNumber] else { return nil }
  guard xy.count == 2 else { fail(key + " must be [x, y]") }
  return CGPoint(x: xy[0].doubleValue * scale, y: xy[1].doubleValue * scale)
}

func need(_ key: String) -> CGPoint {
  guard let at = point(key) else { fail("the input needs " + key + " as [x, y]") }
  return at
}

func here() -> CGPoint {
  return CGEvent(source: nil)!.location
}

// the modifiers named in a "+"-joined text
func mods(_ names: [String]) -> CGEventFlags {
  var flags: CGEventFlags = []
  for mod in names where !mod.isEmpty {
    guard let flag = MODS[mod] else { fail("no modifier " + mod) }
    flags.insert(flag)
  }
  return flags
}

func held() -> CGEventFlags {
  return mods((input["text"] as? String ?? "").lowercased().components(separatedBy: "+"))
}

// posts an event, then gives the system a moment to take it (the
// process may end right after)
func post(_ e: CGEvent, _ flags: CGEventFlags) {
  e.flags = flags
  e.post(tap: .cghidEventTap)
  usleep(10000)
}

func mouse(_ type: CGEventType, _ at: CGPoint, _ button: CGMouseButton, _ clicks: Int64, _ flags: CGEventFlags) {
  let e = CGEvent(mouseEventSource: nil, mouseType: type, mouseCursorPosition: at, mouseButton: button)!
  e.setIntegerValueField(.mouseEventClickState, value: clicks)
  post(e, flags)
}

func click(_ down: CGEventType, _ up: CGEventType, _ button: CGMouseButton, _ times: Int64) {
  let at = point("coordinate") ?? here()
  let flags = held()
  mouse(.mouseMoved, at, button, 0, flags)
  for n in 1...times {
    mouse(down, at, button, n, flags)
    mouse(up, at, button, n, flags)
  }
}

// a key or a "+"-joined combination: its key code and modifiers
func combo(_ str: String) -> (CGKeyCode, CGEventFlags) {
  var names = str.lowercased().components(separatedBy: "+")
  let last = names.removeLast()
  guard let code = KEYS[last] else { fail("no key " + last) }
  return (code, mods(names))
}

func key(_ code: CGKeyCode, _ flags: CGEventFlags, _ down: Bool) {
  post(CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: down)!, flags)
}

// types a text, a few characters per event (an event holds at most 20
// UTF-16 units), a line end as the return key
func type(_ str: String) {
  for (i, line) in str.components(separatedBy: "\n").enumerated() {
    if i > 0 {
      key(KEYS["return"]!, [], true)
      key(KEYS["return"]!, [], false)
    }
    var parts: [[UInt16]] = [[]]
    for char in line {
      let units = Array(String(char).utf16)
      if parts[parts.count - 1].count + units.count > 16 {
        parts.append([])
      }
      parts[parts.count - 1] += units
    }
    for part in parts where !part.isEmpty {
      for down in [true, false] {
        let e = CGEvent(keyboardEventSource: nil, virtualKey: 0, keyDown: down)!
        e.keyboardSetUnicodeString(stringLength: part.count, unicodeString: part)
        post(e, [])
      }
    }
  }
}

var answer = "OK"

switch name {
case "screenshot", "zoom", "wait":
  break
case "left_click":
  click(.leftMouseDown, .leftMouseUp, .left, 1)
case "double_click":
  click(.leftMouseDown, .leftMouseUp, .left, 2)
case "triple_click":
  click(.leftMouseDown, .leftMouseUp, .left, 3)
case "right_click":
  click(.rightMouseDown, .rightMouseUp, .right, 1)
case "middle_click":
  click(.otherMouseDown, .otherMouseUp, .center, 1)
case "mouse_move":
  let down = CGEventSource.buttonState(.combinedSessionState, button: .left)
  mouse(down ? .leftMouseDragged : .mouseMoved, need("coordinate"), .left, down ? 1 : 0, [])
case "left_mouse_down":
  mouse(.leftMouseDown, here(), .left, 1, [])
case "left_mouse_up":
  mouse(.leftMouseUp, here(), .left, 1, [])
case "left_click_drag":
  let a = need("start_coordinate")
  let b = need("coordinate")
  let flags = held()
  mouse(.mouseMoved, a, .left, 0, flags)
  mouse(.leftMouseDown, a, .left, 1, flags)
  for s in 1...10 {
    let t = Double(s) / 10
    mouse(.leftMouseDragged, CGPoint(x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t), .left, 1, flags)
  }
  mouse(.leftMouseUp, b, .left, 1, flags)
case "scroll":
  let at = point("coordinate") ?? here()
  let amount = Int32((input["scroll_amount"] as? NSNumber)?.intValue ?? 1)
  var dy: Int32 = 0
  var dx: Int32 = 0
  switch text("scroll_direction") {
  case "up": dy = amount
  case "down": dy = -amount
  case "left": dx = amount
  case "right": dx = -amount
  default: fail("scroll_direction must be up, down, left or right")
  }
  mouse(.mouseMoved, at, .left, 0, [])
  post(CGEvent(scrollWheelEvent2Source: nil, units: .line, wheelCount: 2, wheel1: dy, wheel2: dx, wheel3: 0)!, held())
case "type":
  type(text("text"))
case "key":
  let (code, flags) = combo(text("text"))
  let times = max(1, min(100, (input["repeat"] as? NSNumber)?.intValue ?? 1))
  for _ in 1...times {
    key(code, flags, true)
    key(code, flags, false)
  }
case "press", "release":
  let (code, flags) = combo(text("text"))
  key(code, flags, name == "press")
case "reset":
  // Release only inputs this interrupted action could have posted.
  let action = input["action"] as? String ?? ""
  if action == "key" {
    let (code, flags) = combo(text("text"))
    key(code, flags, false)
  } else if action == "type" {
    key(0, [], false)
    key(KEYS["return"]!, [], false)
  }
  if action == "right_click" { mouse(.rightMouseUp, here(), .right, 1, []) }
  else if action == "middle_click" { mouse(.otherMouseUp, here(), .center, 1, []) }
  else if ["left_mouse_down", "left_click", "double_click", "triple_click", "left_click_drag"].contains(action) {
    mouse(.leftMouseUp, here(), .left, 1, [])
  }
case "cursor_position":
  let at = here()
  answer = "X=" + String(Int((at.x / scale).rounded())) + ", Y=" + String(Int((at.y / scale).rounded()))
default:
  fail("no computer action " + name)
}

let size = ["w": Int((screen.width / scale).rounded()), "h": Int((screen.height / scale).rounded())]
let out: [String: Any] = ["w": size["w"]!, "h": size["h"]!, "scale": scale, "text": answer]
print(String(data: try! JSONSerialization.data(withJSONObject: out), encoding: .utf8)!)
