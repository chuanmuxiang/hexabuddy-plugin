#!/usr/bin/env python3
"""macOS login Keychain access to the desktop key, through Security.framework (the key never appears in command arguments)."""
import ctypes as c
import sys

SERVICE = 'com.hexabuddy.desktop-api-key'

def keychain(server, value=None, *, delete=False):
    if sys.platform != 'darwin':
        raise RuntimeError('macOS Keychain is required; use HEXABUDDY_API_KEY on other systems')
    cf = c.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
    sec = c.CDLL('/System/Library/Frameworks/Security.framework/Security')
    ptr = c.c_void_p
    cf.CFStringCreateWithCString.argtypes = [ptr, c.c_char_p, c.c_uint32]; cf.CFStringCreateWithCString.restype = ptr
    cf.CFDataCreate.argtypes = [ptr, c.c_char_p, c.c_long]; cf.CFDataCreate.restype = ptr
    cf.CFDictionaryCreate.argtypes = [ptr, ptr, ptr, c.c_long, ptr, ptr]; cf.CFDictionaryCreate.restype = ptr
    cf.CFDataGetLength.argtypes = [ptr]; cf.CFDataGetLength.restype = c.c_long
    cf.CFDataGetBytePtr.argtypes = [ptr]; cf.CFDataGetBytePtr.restype = ptr
    cf.CFRelease.argtypes = [ptr]
    sec.SecItemCopyMatching.argtypes = [ptr, c.POINTER(ptr)]
    sec.SecItemAdd.argtypes = [ptr, ptr]
    sec.SecItemUpdate.argtypes = [ptr, ptr]
    sec.SecItemDelete.argtypes = [ptr]
    owned=[]
    def string(s):
        p=cf.CFStringCreateWithCString(None,s.encode(),0x08000100); owned.append(p); return p
    def constant(s): return ptr.in_dll(sec,s).value
    def dictionary(entries):
        keys=(ptr*len(entries))(*[constant(k) for k,v in entries]); vals=(ptr*len(entries))(*[v for k,v in entries])
        p=cf.CFDictionaryCreate(None,keys,vals,len(entries),None,None); owned.append(p); return p
    try:
        entries=[('kSecClass',constant('kSecClassGenericPassword')),('kSecAttrService',string(SERVICE)),('kSecAttrAccount',string(server.rstrip('/')))]
        if delete:
            status = sec.SecItemDelete(dictionary(entries))
            if status not in (0, -25300): raise RuntimeError("Could not remove desktop key")
            return
        if value is None:
            query=dictionary(entries+[('kSecReturnData',ptr.in_dll(cf,'kCFBooleanTrue').value)])
            result=ptr(); status=sec.SecItemCopyMatching(query,c.byref(result))
            if status == -25300: return ''
            if status: raise RuntimeError('Keychain access failed; unlock your login Keychain and retry')
            owned.append(result.value)
            return c.string_at(cf.CFDataGetBytePtr(result),cf.CFDataGetLength(result)).decode()
        raw=value.encode(); data=cf.CFDataCreate(None,raw,len(raw)); owned.append(data)
        status=sec.SecItemUpdate(dictionary(entries),dictionary([('kSecValueData',data)]))
        if status == -25300:
            status=sec.SecItemAdd(dictionary(entries+[('kSecValueData',data),('kSecAttrAccessible',constant('kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly'))]),None)
        if status: raise RuntimeError('Could not save desktop key in Keychain')
    finally:
        for p in reversed(owned): cf.CFRelease(p)
