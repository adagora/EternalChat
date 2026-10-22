import Foundation

@main struct ConnectionChecks {
    static func main() throws {
        let secret = String(repeating:"a",count:43)
        let (base,token) = try Connection.pairing("https://example.test/#\(secret)")
        precondition(token == secret)
        precondition(base.absoluteString == "https://example.test/")
        let session = Connection(base:base,token:token,id:"test")
        precondition(session.permits(URL(string:"https://example.test/api/state")!))
        precondition(session.permits(URL(string:"https://EXAMPLE.test:443/")!))
        for text in ["https://evil.test/","https://example.test:444/","http://example.test/","https://user@example.test/"] {
            precondition(!session.permits(URL(string:text)!))
        }
        for text in ["http://example.test/#\(secret)","https://user:pass@example.test/#\(secret)","https://example.test/path#\(secret)","https://example.test/?query=1#\(secret)","https://example.test/#bad","https://example.test/#\(secret)%0A","file:///tmp/secret"] {
            do { _ = try Connection.pairing(text); fatalError("Accepted invalid link") }
            catch ConnectionError.invalidLink { }
        }
        let restored = try JSONDecoder().decode(Connection.self,from:JSONEncoder().encode(session))
        precondition(restored == session)
        print("iOS shared connection checks passed (Foundation only; UIKit app not built)")
    }
}
