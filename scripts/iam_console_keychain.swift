// Initial console passwords stay in memory and the local macOS login Keychain.
// The SDK helper receives the password through a memory pipe, never argv/files.
import Foundation
import Security
import CryptoKit

enum Failure: Error { case code(String) }

func emit(_ value: [String: Any]) {
    let data = try! JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
    print(String(data: data, encoding: .utf8)!)
}

func randomPassword() throws -> String {
    var bytes = [UInt8](repeating: 0, count: 32)
    guard SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes) == errSecSuccess else {
        throw Failure.code("secure_random_failed")
    }
    return "Aa1!" + Data(bytes).base64EncodedString()
}

func keyQuery(_ service: String, _ user: String) -> [String: Any] {
    return [kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service, kSecAttrAccount as String: user,
            kSecAttrSynchronizable as String: false]
}

func storedPassword(_ query: [String: Any]) throws -> String? {
    var request = query
    request[kSecReturnData as String] = true
    request[kSecMatchLimit as String] = kSecMatchLimitOne
    var value: CFTypeRef?
    let status = SecItemCopyMatching(request as CFDictionary, &value)
    if status == errSecItemNotFound { return nil }
    guard status == errSecSuccess, let data = value as? Data,
          let password = String(data: data, encoding: .utf8) else {
        throw Failure.code("keychain_read_failed")
    }
    return password
}

func storePassword(_ query: [String: Any], _ password: String) throws {
    var item = query
    item[kSecValueData as String] = password.data(using: .utf8)!
    item[kSecAttrLabel as String] = "Team Bravo initial console password"
    guard SecItemAdd(item as CFDictionary, nil) == errSecSuccess else {
        throw Failure.code("keychain_write_failed")
    }
    guard try storedPassword(query) == password else {
        throw Failure.code("keychain_readback_mismatch")
    }
}

func aws(_ arguments: [String], profile: String?) throws -> (Int32, [String: Any]) {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
    process.arguments = ["aws"] + arguments + ["--region", "ap-northeast-2", "--output", "json", "--no-cli-pager", "--cli-connect-timeout", "10", "--cli-read-timeout", "30"]
    if let profile { process.arguments! += ["--profile", profile] }
    var environment = ProcessInfo.processInfo.environment
    environment["AWS_MAX_ATTEMPTS"] = "1"
    process.environment = environment
    let output = Pipe(), errors = Pipe(), incoming = Pipe()
    process.standardOutput = output; process.standardError = errors; process.standardInput = incoming
    try process.run()
    try incoming.fileHandleForWriting.close()
    let data = output.fileHandleForReading.readDataToEndOfFile()
    let errorData = errors.fileHandleForReading.readDataToEndOfFile()
    process.waitUntilExit()
    if process.terminationStatus != 0 {
        let message = String(data: errorData, encoding: .utf8) ?? ""
        let regex = try NSRegularExpression(pattern: "An error occurred \\(([A-Za-z0-9]+)\\)")
        if let match = regex.firstMatch(in: message, range: NSRange(message.startIndex..., in: message)),
           let range = Range(match.range(at: 1), in: message) {
            return (process.terminationStatus, ["error_code": String(message[range])])
        }
        return (process.terminationStatus, ["error_code": "aws_cli_failed"])
    }
    return (0, (try JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:])
}

func sdk(_ python: String, _ script: String, input: [String: Any]? = nil) throws -> [String: Any] {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: python)
    process.arguments = ["-I", script] + (input == nil ? ["--probe"] : [])
    let output = Pipe(), errors = Pipe(), incoming = Pipe()
    process.standardOutput = output; process.standardError = errors; process.standardInput = incoming
    try process.run()
    if let input { incoming.fileHandleForWriting.write(try JSONSerialization.data(withJSONObject: input)) }
    try incoming.fileHandleForWriting.close()
    let data = output.fileHandleForReading.readDataToEndOfFile()
    _ = errors.fileHandleForReading.readDataToEndOfFile()
    process.waitUntilExit()
    guard process.terminationStatus == 0,
          let result = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
        throw Failure.code("sdk_profile_operation_failed")
    }
    return result
}

do {
    // Do not leave unattended sessions waiting on a keychain authorization dialog.
    guard SecKeychainSetUserInteractionAllowed(false) == errSecSuccess else {
        throw Failure.code("keychain_noninteractive_mode_failed")
    }
    if CommandLine.arguments.contains("--probe") {
        let query = keyQuery("team-bravo.iam.probe." + UUID().uuidString, "probe")
        let password = try randomPassword()
        try storePassword(query, password)
        guard SecItemDelete(query as CFDictionary) == errSecSuccess else {
            throw Failure.code("keychain_probe_cleanup_failed")
        }
        emit(["keychain_probe": "PASS", "secret_output": false])
    } else {
        let data = FileHandle.standardInput.readDataToEndOfFile()
        guard let config = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              let user = config["user_name"] as? String,
              let expected = config["expected_account"] as? String else {
            throw Failure.code("invalid_input")
        }
        let profile = config["profile"] as? String
        guard let python = config["sdk_python"] as? String,
              let script = config["sdk_script"] as? String,
              try sdk(python, script)["sdk_probe"] as? String == "PASS" else {
            throw Failure.code("sdk_probe_required")
        }
        if CommandLine.arguments.contains("--transport-probe") {
            emit(try sdk(python, script, input: ["transport_probe": true])); exit(0)
        }
        let (identityStatus, identity) = try aws(["sts", "get-caller-identity"], profile: profile)
        guard identityStatus == 0, identity["Account"] as? String == expected else {
            throw Failure.code("account_boundary_failed")
        }
        let (userStatus, userResult) = try aws(["iam", "get-user", "--user-name", user], profile: profile)
        guard userStatus == 0, let target = userResult["User"] as? [String: Any],
              target["Path"] as? String == "/team-bravo/humans/",
              let tags = target["Tags"] as? [[String: String]],
              tags.contains(where: { $0["Key"] == "principal-type" && $0["Value"] == "human" }),
              tags.contains(where: { $0["Key"] == "project" && $0["Value"] == "softbank-finals" }) else {
            throw Failure.code("target_not_managed_human")
        }
        let (loginStatus, login) = try aws(["iam", "get-login-profile", "--user-name", user], profile: profile)
        if loginStatus == 0 {
            emit(["console_profile": "existing_preserved", "login_verified": false])
        } else {
            guard login["error_code"] as? String == "NoSuchEntity" else {
                throw Failure.code("login_profile_read_failed")
            }
            let scope = SHA256.hash(data: Data(expected.utf8)).prefix(8).map { String(format: "%02x", $0) }.joined()
            let query = keyQuery("team-bravo.iam-initial." + scope, user)
            let password: String
            if let existing = try storedPassword(query) {
                password = existing
            } else {
                password = try randomPassword()
                try storePassword(query, password)
            }
            var request: [String: Any] = ["expected_account": expected, "user_name": user, "password": password]
            if let profile { request["profile"] = profile }
            let result = try sdk(python, script, input: request)
            guard result["console_profile"] as? String == "created" else {
                emit(["console_profile": "existing_preserved", "login_verified": false]); exit(0)
            }
            let (readStatus, readback) = try aws(["iam", "get-login-profile", "--user-name", user], profile: profile)
            guard readStatus == 0, let returned = readback["LoginProfile"] as? [String: Any],
                  returned["PasswordResetRequired"] as? Bool == true,
                  try storedPassword(query) == password else {
                throw Failure.code("profile_readback_failed")
            }
            emit(["console_profile": "created", "password_reset_required": true,
                  "keychain_readback": "PASS", "login_verified": false, "secret_output": false])
        }
    }
} catch Failure.code(let code) {
    emit(["error_code": code]); exit(1)
} catch {
    emit(["error_code": "native_helper_failed"]); exit(1)
}
