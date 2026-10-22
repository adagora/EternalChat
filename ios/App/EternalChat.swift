import SwiftUI
import UIKit
import WebKit
import UserNotifications

@main @MainActor struct EternalChatApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) var delegate
    @StateObject private var session = Session.shared
    @State private var link = ""
    var body: some Scene {
        WindowGroup {
            NavigationStack {
                Group {
                    if let connection = session.connection {
                        ChatPage(connection:connection, session:session)
                            .toolbar {
                                ToolbarItem(placement:.topBarTrailing) {
                                    Menu {
                                        Button(session.notifications ? "Disable notifications" : "Enable notifications") {
                                            Task { if session.notifications { await session.disableNotifications() } else { await session.enableNotifications() } }
                                        }
                                        Button("Reload") { session.reload = UUID() }
                                        Button("Revoke this device", role:.destructive) { Task { await session.logout() } }
                                    } label: { Image(systemName:"ellipsis.circle").accessibilityLabel("App settings") }
                                }
                            }
                    } else {
                        Form {
                            Section("Pair this iPhone or iPad") {
                                Text("Create a pairing link in EternalChat Settings on a connected browser. The server must use HTTPS.")
                                SecureField("HTTPS pairing link",text:$link).textInputAutocapitalization(.never).autocorrectionDisabled()
                                Button(session.busy ? "Connecting…" : "Connect") { let candidate = link; link = ""; Task { await session.pair(candidate) } }.disabled(session.busy || link.isEmpty)
                            }
                        }.navigationTitle("EternalChat")
                    }
                }
                .sheet(item:$session.exportFile) { file in ExportSheet(file:file.url) }
                .safeAreaInset(edge:.bottom) {
                    if !session.message.isEmpty {
                        HStack { Text(session.message).font(.footnote); Button("Dismiss") { session.message = "" } }.padding().background(.regularMaterial)
                    }
                }
            }
        }
    }
}

@MainActor final class AppDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    func application(_ application: UIApplication, didFinishLaunchingWithOptions options:[UIApplication.LaunchOptionsKey:Any]? = nil) -> Bool {
        UNUserNotificationCenter.current().delegate = self
        if UserDefaults.standard.bool(forKey:"native-notifications") { application.registerForRemoteNotifications() }
        return true
    }
    func application(_ application:UIApplication, didRegisterForRemoteNotificationsWithDeviceToken token:Data) {
        Task { @MainActor in await Session.shared.registered(token) }
    }
    func application(_ application:UIApplication, didFailToRegisterForRemoteNotificationsWithError error:Error) {
        Task { @MainActor in Session.shared.message = "APNs registration failed. Check signing, push entitlement and network." }
    }
    func userNotificationCenter(_ center:UNUserNotificationCenter, didReceive response:UNNotificationResponse,
                                withCompletionHandler completionHandler:@escaping () -> Void) {
        Task { @MainActor in Session.shared.reload = UUID(); completionHandler() }
    }
    func userNotificationCenter(_ center:UNUserNotificationCenter, willPresent notification:UNNotification,
                                withCompletionHandler completionHandler:@escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([]) // the foreground page already shows the reply
    }
}

struct ExportSheet: UIViewControllerRepresentable {
    let file:URL
    func makeUIViewController(context:Context) -> UIActivityViewController {
        let controller = UIActivityViewController(activityItems:[file],applicationActivities:nil)
        if let popover = controller.popoverPresentationController {
            popover.sourceView = controller.view
            popover.sourceRect = CGRect(x:controller.view.bounds.midX,y:controller.view.bounds.midY,width:1,height:1)
            popover.permittedArrowDirections = []
        }
        controller.completionWithItemsHandler = { _,_,_,_ in try? FileManager.default.removeItem(at:file.deletingLastPathComponent()) }
        return controller
    }
    func updateUIViewController(_ controller:UIActivityViewController,context:Context) {}
}

struct ChatPage: UIViewRepresentable {
    let connection: Connection
    @ObservedObject var session: Session
    func makeCoordinator() -> Coordinator { Coordinator(connection,session) }
    func makeUIView(context:Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        let encoded = { (value:String) -> String in String(data:try! JSONSerialization.data(withJSONObject:value,options:.fragmentsAllowed),encoding:.utf8)! }
        let base = encoded(connection.base.absoluteString), token = encoded(connection.token)
        let script = """
        if(location.origin === new URL(\(base)).origin){
          localStorage.setItem('eternalchat-token',\(token));
          const remove=Storage.prototype.removeItem;
          Storage.prototype.removeItem=function(key){ remove.call(this,key); if(key==='eternalchat-token' && this===localStorage) window.webkit.messageHandlers.eternalChat.postMessage('forgot'); };
        }
        """
        config.userContentController.addUserScript(WKUserScript(source:script,injectionTime:.atDocumentStart,forMainFrameOnly:true))
        config.userContentController.add(context.coordinator,name:"eternalChat")
        let view = WKWebView(frame:.zero,configuration:config)
        view.navigationDelegate = context.coordinator; view.uiDelegate = context.coordinator
        view.allowsBackForwardNavigationGestures = false
        context.coordinator.reload = session.reload
        view.load(URLRequest(url:connection.base))
        return view
    }
    func updateUIView(_ view:WKWebView,context:Context) {
        if context.coordinator.reload != session.reload { context.coordinator.reload = session.reload; view.reload() }
    }
    static func dismantleUIView(_ view:WKWebView,coordinator:Coordinator) {
        view.stopLoading(); view.configuration.userContentController.removeScriptMessageHandler(forName:"eternalChat")
    }
    @MainActor final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler, WKDownloadDelegate {
        let connection:Connection
        let session:Session
        var reload = UUID()
        var downloads:[ObjectIdentifier:URL] = [:]
        init(_ connection:Connection,_ session:Session) { self.connection = connection; self.session = session }
        func webView(_ view:WKWebView,decidePolicyFor action:WKNavigationAction,decisionHandler:@escaping (WKNavigationActionPolicy)->Void) {
            guard let url = action.request.url else { decisionHandler(.cancel); return }
            if url.scheme == "blob", let nested = URL(string:String(url.absoluteString.dropFirst(5))), connection.permits(nested) {
                decisionHandler(.download)
            } else if connection.permits(url) { decisionHandler(action.shouldPerformDownload ? .download : .allow) }
            else {
                decisionHandler(.cancel)
                if action.navigationType == .linkActivated, ["https","http","mailto"].contains(url.scheme?.lowercased() ?? "") { UIApplication.shared.open(url) }
            }
        }
        func webView(_ view:WKWebView,decidePolicyFor response:WKNavigationResponse,decisionHandler:@escaping (WKNavigationResponsePolicy)->Void) {
            if !response.canShowMIMEType, let url = response.response.url, connection.permits(url) { decisionHandler(.download) }
            else { decisionHandler(.allow) }
        }
        func webView(_ view:WKWebView,navigationAction:WKNavigationAction,didBecome download:WKDownload) { download.delegate = self }
        func webView(_ view:WKWebView,navigationResponse:WKNavigationResponse,didBecome download:WKDownload) { download.delegate = self }
        func download(_ download:WKDownload,decideDestinationUsing response:URLResponse,suggestedFilename:String,completionHandler:@escaping (URL?)->Void) {
            do {
                let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString,isDirectory:true)
                try FileManager.default.createDirectory(at:folder,withIntermediateDirectories:true,attributes:[.posixPermissions:0o700])
                let name = (suggestedFilename as NSString).lastPathComponent
                let target = folder.appendingPathComponent(["", ".", ".."].contains(name) ? "EternalChat-memory.zip" : name)
                downloads[ObjectIdentifier(download)] = target
                completionHandler(target)
            } catch { session.message = "Could not prepare the export file."; completionHandler(nil) }
        }
        func downloadDidFinish(_ download:WKDownload) {
            if let url = downloads.removeValue(forKey:ObjectIdentifier(download)) { session.exportFile = ExportFile(url:url) }
        }
        func download(_ download:WKDownload,didFailWithError error:Error,resumeData:Data?) {
            if let url = downloads.removeValue(forKey:ObjectIdentifier(download)) { try? FileManager.default.removeItem(at:url.deletingLastPathComponent()) }
            session.message = "Export failed. Reconnect and try again."
        }
        func webView(_ view:WKWebView,createWebViewWith configuration:WKWebViewConfiguration,for action:WKNavigationAction,windowFeatures:WKWindowFeatures) -> WKWebView? {
            if let url = action.request.url {
                if connection.permits(url) { view.load(URLRequest(url:url)) }
                else if ["https","http","mailto"].contains(url.scheme?.lowercased() ?? "") { UIApplication.shared.open(url) }
            }
            return nil
        }
        func webView(_ view:WKWebView,requestMediaCapturePermissionFor origin:WKSecurityOrigin,initiatedByFrame frame:WKFrameInfo,type:WKMediaCaptureType,decisionHandler:@escaping (WKPermissionDecision)->Void) {
            let same = origin.protocol == connection.base.scheme && origin.host.lowercased() == connection.base.host?.lowercased() && (origin.port == 0 ? 443 : origin.port) == (connection.base.port ?? 443)
            decisionHandler(same && frame.isMainFrame ? .prompt : .deny)
        }
        func userContentController(_ controller:WKUserContentController,didReceive message:WKScriptMessage) {
            guard message.frameInfo.isMainFrame, let url = message.frameInfo.request.url,
                  connection.permits(url), message.body as? String == "forgot" else { return }
            session.forgetLocally()
        }
    }
}
