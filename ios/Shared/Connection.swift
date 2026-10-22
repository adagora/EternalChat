import Foundation

struct Connection: Codable, Equatable {
    let base: URL
    let token: String
    let id: String

    static func pairing(_ text: String) throws -> (URL, String) {
        guard text.utf8.count <= 4096,
              var parts = URLComponents(string: text.trimmingCharacters(in: .whitespacesAndNewlines)),
              parts.scheme?.lowercased() == "https", let host = parts.host, !host.isEmpty,
              parts.user == nil, parts.password == nil, parts.query == nil,
              parts.path.isEmpty || parts.path == "/", let token = parts.fragment,
              (32...256).contains(token.utf8.count),
              token.utf8.allSatisfy({ (48...57).contains($0) || (65...90).contains($0) || (97...122).contains($0) || [45,46,95].contains($0) }) else {
            throw ConnectionError.invalidLink
        }
        parts.fragment = nil
        parts.path = "/"
        guard let base = parts.url else { throw ConnectionError.invalidLink }
        return (base, token)
    }

    func permits(_ url: URL) -> Bool {
        url.scheme?.lowercased() == base.scheme?.lowercased()
            && url.host?.lowercased() == base.host?.lowercased()
            && (url.port ?? 443) == (base.port ?? 443)
            && url.user == nil && url.password == nil
    }
}

enum ConnectionError: LocalizedError {
    case invalidLink, server, rejected, storage
    var errorDescription: String? {
        switch self {
        case .invalidLink: return "Paste the complete HTTPS pairing link from EternalChat Settings."
        case .server: return "Cannot connect to this EternalChat server. Check its HTTPS address and network."
        case .rejected: return "This pairing link or device session expired or was revoked. Create a new pairing link."
        case .storage: return "Cannot save this device session in Keychain."
        }
    }
}
