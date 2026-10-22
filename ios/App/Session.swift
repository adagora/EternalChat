import Foundation
import Security
import SwiftUI
import UIKit
import UserNotifications
import WebKit

final class NoRedirects: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}

struct ExportFile: Identifiable { let id = UUID(); let url: URL }

@MainActor final class Session: ObservableObject {
    static let shared = Session()
    @Published var connection: Connection?
    @Published var message = ""
    @Published var busy = false
    @Published var reload = UUID()
    @Published var exportFile: ExportFile?
    @Published var notifications = UserDefaults.standard.bool(forKey: "native-notifications")
    private let service = "EternalChat.session"
    private let redirects = NoRedirects()
    private lazy var network: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 20
        config.timeoutIntervalForResource = 30
        config.httpShouldSetCookies = false
        return URLSession(configuration: config, delegate: redirects, delegateQueue: nil)
    }()

    init() {
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: "EternalChat.session", kSecAttrAccount as String: "connection",
            kSecReturnData as String: true, kSecMatchLimit as String: kSecMatchLimitOne]
        var result: CFTypeRef?
        if SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess, let data = result as? Data,
           let saved = try? JSONDecoder().decode(Connection.self, from: data), saved.base.scheme == "https" {
            connection = saved
        }
    }

    func request(_ base: URL, _ token: String, _ path: String, body: [String: Any]? = nil) async throws -> [String: Any] {
        guard let url = URL(string: path, relativeTo: base)?.absoluteURL,
              url.scheme == "https", url.host == base.host, url.port == base.port else { throw ConnectionError.server }
        var request = URLRequest(url: url)
        request.setValue(token, forHTTPHeaderField: "X-EternalChat-Token")
        if let body {
            request.httpMethod = "POST"
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        let (data, response) = try await network.data(for: request)
        guard let response = response as? HTTPURLResponse else { throw ConnectionError.server }
        if response.statusCode == 403 { throw ConnectionError.rejected }
        guard response.statusCode == 200, data.count <= 1_000_000,
              let value = try JSONSerialization.jsonObject(with: data) as? [String: Any] else { throw ConnectionError.server }
        return value
    }

    func pair(_ link: String) async {
        guard !busy else { return }
        busy = true; message = ""
        defer { busy = false }
        do {
            let (base, candidate) = try Connection.pairing(link)
            let credential: String
            if candidate.hasPrefix("ecs.") { credential = candidate }
            else {
                let response = try await request(base, candidate, "/api/sessions/open", body: ["label": "iOS companion"])
                guard let token = response["token"] as? String else { throw ConnectionError.server }
                credential = token
            }
            let state = try await request(base, credential, "/api/sessions")
            guard let id = state["current"] as? String else { throw ConnectionError.server }
            let saved = Connection(base: base, token: credential, id: id)
            let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
                kSecAttrService as String: service, kSecAttrAccount as String: "connection"]
            var item = query
            item[kSecValueData as String] = try JSONEncoder().encode(saved)
            item[kSecAttrAccessible as String] = kSecAttrAccessibleWhenUnlockedThisDeviceOnly
            let update: [String: Any] = [kSecValueData as String: item[kSecValueData as String]!,
                                       kSecAttrAccessible as String: kSecAttrAccessibleWhenUnlockedThisDeviceOnly]
            let status = SecItemUpdate(query as CFDictionary, update as CFDictionary)
            guard status == errSecSuccess || (status == errSecItemNotFound && SecItemAdd(item as CFDictionary, nil) == errSecSuccess) else { throw ConnectionError.storage }
            connection = saved
            if notifications { UIApplication.shared.registerForRemoteNotifications() }
        } catch { message = (error as? ConnectionError)?.localizedDescription ?? ConnectionError.server.localizedDescription }
    }

    func forgetLocally() {
        SecItemDelete([kSecClass as String:kSecClassGenericPassword, kSecAttrService as String:service,
                       kSecAttrAccount as String:"connection"] as CFDictionary)
        connection = nil; notifications = false
        UserDefaults.standard.set(false, forKey: "native-notifications")
        UIApplication.shared.unregisterForRemoteNotifications()
        WKWebsiteDataStore.default().removeData(ofTypes: WKWebsiteDataStore.allWebsiteDataTypes(), modifiedSince: .distantPast, completionHandler: {})
    }

    func logout() async {
        guard let current = connection else { return }
        do {
            _ = try await request(current.base, current.token, "/api/sessions/revoke", body:["id":current.id])
            forgetLocally()
        } catch ConnectionError.rejected { forgetLocally() }
        catch { message = "Reconnect before revoking this device. You can also revoke it from another paired browser." }
    }

    func enableNotifications() async {
        guard let current = connection else { return }
        do {
            let status = try await request(current.base, current.token, "/api/native-push")
            guard status["configured"] as? Bool == true, status["topic"] as? String == Bundle.main.bundleIdentifier else {
                message = "Configure this app's APNs key, team and bundle topic on the server first."; return
            }
            let granted = try await UNUserNotificationCenter.current().requestAuthorization(options:[.alert,.sound,.badge])
            guard granted else { message = "Allow notifications for EternalChat in iOS Settings."; return }
            notifications = true; UserDefaults.standard.set(true, forKey:"native-notifications")
            UIApplication.shared.registerForRemoteNotifications()
        } catch { message = "Could not enable notifications. Check the server configuration and network." }
    }

    func registered(_ data: Data) async {
        guard notifications, let current = connection else { return }
        let token = data.map { String(format:"%02x",$0) }.joined()
        do { _ = try await request(current.base, current.token, "/api/native-push/subscribe", body:["token":token]); message = "Native push registered. Delivery is controlled by Apple and iOS." }
        catch { message = "Could not register native push. Check APNs configuration, then enable notifications again." }
    }

    func disableNotifications() async {
        guard let current = connection else { return }
        do {
            _ = try await request(current.base,current.token,"/api/push/unsubscribe",body:[:])
            notifications = false; UserDefaults.standard.set(false,forKey:"native-notifications")
            UIApplication.shared.unregisterForRemoteNotifications()
        } catch { message = "Reconnect to turn off server notifications for this device." }
    }
}
