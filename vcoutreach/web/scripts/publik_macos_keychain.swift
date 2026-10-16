#!/usr/bin/env swift
import Foundation
import Security

func fail(_ code: String) -> Never {
    FileHandle.standardError.write(Data((code + "\n").utf8))
    exit(2)
}

let arguments = CommandLine.arguments
guard arguments.count == 4, ["store", "load"].contains(arguments[1]) else {
    fail("invalid_arguments")
}

let action = arguments[1]
let service = arguments[2]
let account = arguments[3]
guard !service.isEmpty, !account.isEmpty, service.utf8.count <= 255, account.utf8.count <= 255 else {
    fail("invalid_arguments")
}

let query: [String: Any] = [
    kSecClass as String: kSecClassGenericPassword,
    kSecAttrService as String: service,
    kSecAttrAccount as String: account,
]

if action == "store" {
    let value = FileHandle.standardInput.readDataToEndOfFile()
    guard !value.isEmpty, value.count <= 8192, String(data: value, encoding: .utf8) != nil else {
        fail("invalid_value")
    }
    let update: [String: Any] = [
        kSecValueData as String: value,
        kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
    ]
    let updated = SecItemUpdate(query as CFDictionary, update as CFDictionary)
    if updated == errSecSuccess {
        exit(0)
    }
    guard updated == errSecItemNotFound else {
        fail("keychain_store_failed")
    }
    var added = query
    added[kSecValueData as String] = value
    added[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
    guard SecItemAdd(added as CFDictionary, nil) == errSecSuccess else {
        fail("keychain_store_failed")
    }
    exit(0)
}

var lookup = query
lookup[kSecReturnData as String] = true
lookup[kSecMatchLimit as String] = kSecMatchLimitOne
var result: CFTypeRef?
let status = SecItemCopyMatching(lookup as CFDictionary, &result)
guard status == errSecSuccess else {
    fail(status == errSecItemNotFound ? "keychain_item_missing" : "keychain_load_failed")
}
guard let value = result as? Data, !value.isEmpty, value.count <= 8192 else {
    fail("keychain_load_failed")
}
FileHandle.standardOutput.write(value)
