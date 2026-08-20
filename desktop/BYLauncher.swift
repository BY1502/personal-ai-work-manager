import Foundation

guard let resourceURL = Bundle.main.resourceURL else {
    exit(1)
}

let launcher = resourceURL.appendingPathComponent("launcher.sh")
let process = Process()
process.executableURL = URL(fileURLWithPath: "/bin/zsh")
process.arguments = [launcher.path]
process.environment = ProcessInfo.processInfo.environment

do {
    try process.run()
    process.waitUntilExit()
    exit(process.terminationStatus)
} catch {
    exit(1)
}
