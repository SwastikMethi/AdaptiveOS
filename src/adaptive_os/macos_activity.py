"""Scoped App Nap protection without preventing idle system/display sleep.

Use the system Objective-C runtime instead of adding a Python Cocoa dependency.
All native objects/calls stay on the session-owner thread. The autoreleased
activity is retained across calls and released only after endActivity:.
"""

import ctypes
import sys


class SessionActivity:
    def __init__(self):
        self._objc = None
        self._token = None
        self._process = None

    @property
    def active(self):
        return self._token is not None

    def _load(self):
        self._foundation = ctypes.CDLL('/System/Library/Frameworks/Foundation.framework/Foundation')
        self._objc = ctypes.CDLL('/usr/lib/libobjc.A.dylib')
        for name in ('objc_getClass', 'sel_registerName'):
            function = getattr(self._objc, name)
            function.argtypes = [ctypes.c_char_p]
            function.restype = ctypes.c_void_p
        # Fixed signatures are required for objc_msgSend on arm64; never use varargs.
        pointer = ctypes.c_void_p
        def message(result, *arguments):
            return ctypes.CFUNCTYPE(result, pointer, pointer, *arguments)(('objc_msgSend', self._objc))
        self._id = message(pointer)
        self._string = message(pointer, ctypes.c_char_p)
        self._begin = message(pointer, ctypes.c_uint64, pointer)
        self._end = message(None, pointer)
        self._void = message(None)

    def begin(self):
        if sys.platform != 'darwin' or self.active:
            return
        if self._objc is None:
            self._load()
        selector = self._objc.sel_registerName
        pool = self._id(self._objc.objc_getClass(b'NSAutoreleasePool'), selector(b'new'))
        try:
            self._process = self._id(self._objc.objc_getClass(b'NSProcessInfo'), selector(b'processInfo'))
            reason = self._string(self._objc.objc_getClass(b'NSString'), selector(b'stringWithUTF8String:'),
                                  b'Adaptive OS sandbox session')
            # NSActivityUserInitiatedAllowingIdleSystemSleep (Foundation/NSProcessInfo.h).
            options = 0x00FFFFFF & ~(1 << 20)
            token = self._begin(self._process, selector(b'beginActivityWithOptions:reason:'), options, reason)
            if not token:
                raise RuntimeError('Could not protect the sandbox session from App Nap')
            self._token = self._id(token, selector(b'retain'))
        finally:
            self._void(pool, selector(b'drain'))

    def end(self):
        if not self.active:
            return
        selector = self._objc.sel_registerName
        token, self._token = self._token, None
        pool = self._id(self._objc.objc_getClass(b'NSAutoreleasePool'), selector(b'new'))
        try:
            self._end(self._process, selector(b'endActivity:'), token)
        finally:
            self._void(token, selector(b'release'))
            self._void(pool, selector(b'drain'))
