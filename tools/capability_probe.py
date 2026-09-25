"""Adversarial probe for the run_blender_python capability boundary.

Run headless:

    BLENDER_USER_CONFIG=/tmp/bc-t13-cfg \
      /Applications/Blender.app/Contents/MacOS/Blender --factory-startup -b \
      -P tools/capability_probe.py

Three sections:

  1. BASELINE - the canaries work with no guard (control).
  2. ATTACKS  - the same attempts inside the reference guard; scored
                ESCAPED / denied / blocked / allowed-by-design.
  3. INTROSPECTION - escapes the reference guard does NOT claim to close,
                demonstrated so the answer can say "open" with evidence.

The guard here is a *reference implementation of the proposed spec*, so that
"denied" in the answer means "this exact mechanism denied it", not "a sentence
says so". Confined to /tmp/bc-t13. No user file or preference is touched.
Output: one JSON blob between PROBE_BEGIN/PROBE_END.
"""

import json
import os
import sys
import types
import builtins as real_builtins

CANARY = "/tmp/bc-t13"
os.makedirs(CANARY, exist_ok=True)
RESULTS = {"baseline": [], "attacks": [], "introspection": [], "notes": []}


def canary(name):
    return os.path.join(CANARY, "canary_" + name)


# ---------------------------------------------------------------------------
# THE CAPABILITY SPEC - single source of truth (proposed blender_copilot/
# capability.py).  Prompt line, namespace builder and tool_argument_error text
# all derive from this block.
# ---------------------------------------------------------------------------

ALLOWED_BUILTINS = frozenset(
    """
    abs aiter all anext any ascii bin bool bytearray bytes callable chr
    classmethod complex delattr dict dir divmod enumerate filter float format
    frozenset getattr hasattr hash hex id int isinstance issubclass iter len
    list map max memoryview min next object oct ord pow print property range
    repr reversed round set setattr slice sorted staticmethod str sum super
    tuple type zip __build_class__
    BaseException Exception ArithmeticError AssertionError AttributeError
    BlockingIOError BufferError BytesWarning ChildProcessError ConnectionError
    DeprecationWarning EOFError EnvironmentError FileExistsError
    FileNotFoundError FloatingPointError GeneratorExit ImportError
    ImportWarning IndentationError IndexError InterruptedError
    IsADirectoryError KeyError KeyboardInterrupt LookupError MemoryError
    ModuleNotFoundError NameError NotADirectoryError NotImplementedError
    OSError OverflowError PendingDeprecationWarning PermissionError
    ProcessLookupError RecursionError ReferenceError ResourceWarning
    RuntimeError RuntimeWarning StopAsyncIteration StopIteration SyntaxError
    SyntaxWarning SystemError SystemExit TabError TimeoutError TypeError
    UnboundLocalError UnicodeDecodeError UnicodeEncodeError UnicodeError
    UnicodeTranslateError UnicodeWarning UserWarning ValueError Warning
    ZeroDivisionError Ellipsis NotImplemented True False None
    """.split()
)

# Compute-only stdlib.  Every name is checked by guarded_import.
ALLOWED_MODULES = frozenset(
    {
        "math", "cmath", "statistics", "random", "itertools", "functools",
        "collections", "heapq", "bisect", "array", "decimal", "fractions",
        "numbers", "re", "string", "textwrap", "unicodedata", "json",
        "dataclasses", "enum", "typing", "abc", "copy", "operator", "uuid",
        "datetime", "time", "hashlib", "base64", "struct", "zlib", "csv",
        "mathutils",
        "numpy",  # bundled 2.3.4
    }
)

# Attribute names denied on EVERY proxied module, because the real module's
# __dict__ holds them (measured: statistics.sys, random._os, fractions.sys,
# dataclasses.inspect, enum.sys, typing.sys, uuid.os).  This is why modules are
# proxied rather than handed over raw.
MODULE_DENY = frozenset(
    {
        "sys", "os", "_os", "inspect", "ctypes", "ctypeslib", "builtins",
        "importlib", "gc", "subprocess", "socket", "io", "_io", "pathlib",
        "shutil", "tempfile", "threading", "posix", "signal", "select",
        "resource", "mmap", "pickle", "marshal", "code", "codeop", "runpy",
        "webbrowser", "urllib", "http", "ssl", "traceback", "linecache",
        "jit", "distutils", "setuptools", "dill", "cloudpickle",
        # numpy filesystem surface (measured: numpy.save wrote a .npy; the
        # module proxies only renamed the leak until these were denied too)
        "save", "savez", "savez_compressed", "load", "fromfile", "memmap",
        "loadtxt", "savetxt", "genfromtxt", "DataSource", "f2py", "testing",
        "lib", "core",
    }
)

ALLOWED_BPY_ATTRS = frozenset({"data", "context", "types", "props", "path", "ops", "app"})

DENIED_OPS_FAMILIES = frozenset(
    {
        "wm", "script", "text", "text_editor", "image", "render", "file",
        "preferences", "extensions", "console", "ed",
        "export_scene", "import_scene", "export_anim", "import_anim",
        "import_curve", "export_mesh", "import_mesh", "sound", "clip",
        "asset", "pack", "screenshot", "grease_pencil", "outliner",
    }
)

DENIED_CONTEXT_ATTRS = frozenset(
    {
        "preferences", "window_manager", "window", "screen", "area", "region",
        "space_data", "workspace", "file", "asset_library_ref",
    }
)

DENIED_APP_ATTRS = frozenset({"timers", "handlers", "driver_namespace", "binary_path"})

DENIED_DATA_ATTRS = frozenset({"libraries"})

DENIED_COLLECTION_METHODS = {
    "images": frozenset({"load", "open", "save", "save_all_modified", "save_sequence"}),
    "texts": frozenset({"load", "open"}),
    "sounds": frozenset({"load"}),
    "fonts": frozenset({"load"}),
    "movieclips": frozenset({"load"}),
    "volumes": frozenset({"load"}),
    "cachefiles": frozenset({"load"}),
    "brushes": frozenset({"load"}),
}

DENIED_DATABLOCK_METHODS = {
    "images": frozenset(
        {"save", "save_render", "save_render_layer", "save_sequence", "pack", "unpack"}
    ),
    "texts": frozenset({"write"}),
}


class CapabilityDenied(Exception):
    def __init__(self, name, hint=""):
        self.capability = name
        self.hint = hint
        super().__init__(
            "capability_denied: %r is not available to run_blender_python "
            "(scene data only).%s" % (name, (" " + hint) if hint else "")
        )


# ---------------------------------------------------------------------------
# Mechanism: restricted builtins + guarded __import__ + closed proxies.
# NO sys.meta_path finder: measured, it fails closed on legitimate transitive
# imports (numpy imports contextvars) and closes nothing the namespace
# __import__ does not already close.
# ---------------------------------------------------------------------------


def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    root = (name or "").split(".")[0]
    if level != 0 or root not in ALLOWED_MODULES:
        raise CapabilityDenied("import %s" % name)
    mod = real_builtins.__import__(name, globals, locals, fromlist, level)
    return _module_proxy(mod, root)


def _make_proxy(real, get, dirnames, name, wrap=None):
    """Closure-based proxy: `real` is a free variable of __getattr__, not an
    instance attribute, so proxy._real does not work.  (It is still reachable
    through type(p).__getattr__.__closure__ - see INTROSPECTION.)"""

    class _P:
        __slots__ = ()

        def __getattr__(self, attr):
            return get(attr)

        def __setattr__(self, attr, value):
            if attr.startswith("_"):
                raise AttributeError(attr)
            setattr(real, attr, value)

        def __dir__(self):
            return sorted(dirnames)

        def __repr__(self):
            return "<scoped %s>" % name

        def __len__(self):
            return len(real)

        def __iter__(self):
            for item in real:
                yield wrap(item) if wrap else item

        def __getitem__(self, key):
            out = real[key]
            return wrap(out) if wrap else out

        def __contains__(self, key):
            return key in real

        def __call__(self, *a, **k):
            return real(*a, **k)

    return _P()


def _module_proxy(real, name):
    def get(attr):
        if attr.startswith("_") or attr in MODULE_DENY:
            raise CapabilityDenied("attribute %s.%s" % (name, attr))
        val = getattr(real, attr)
        if isinstance(val, types.ModuleType):
            return _module_proxy(val, name + "." + attr)
        return val

    return _make_proxy(
        real, get, [a for a in dir(real) if not a.startswith("_")], name
    )


def _ops_get(family):
    if family in DENIED_OPS_FAMILIES:
        raise CapabilityDenied("bpy.ops.%s" % family)
    real_sub = getattr(_REAL_OPS, family)

    def get(attr):
        if attr.startswith("_"):
            raise AttributeError(attr)
        return getattr(real_sub, attr)

    return _make_proxy(real_sub, get, [a for a in dir(real_sub) if not a.startswith("_")], "bpy.ops." + family)


def _bpy_get(attr):
    if attr not in ALLOWED_BPY_ATTRS:
        raise CapabilityDenied("bpy.%s" % attr)
    if attr == "ops":
        return _make_proxy(_REAL_OPS, _ops_get, [a for a in dir(_REAL_OPS) if not a.startswith("_")], "bpy.ops")
    if attr == "app":
        return _app_proxy()
    if attr == "data":
        return _data_proxy()
    if attr == "context":
        return _context_proxy(_REAL_BPY.context)
    if attr in ("path", "props", "types"):
        return _module_proxy(getattr(_REAL_BPY, attr), "bpy." + attr)
    return getattr(_REAL_BPY, attr)


def _app_proxy():
    real = _REAL_BPY.app

    def get(attr):
        if attr in DENIED_APP_ATTRS:
            raise CapabilityDenied("bpy.app.%s" % attr)
        return getattr(real, attr)

    return _make_proxy(real, get, [a for a in dir(real) if not a.startswith("_")], "bpy.app")


def _wrap_datablock(item, coll_name):
    deny = DENIED_DATABLOCK_METHODS.get(coll_name, frozenset())

    def get(attr):
        if attr.startswith("_"):
            raise AttributeError(attr)
        if attr in deny:
            raise CapabilityDenied("bpy.data.%s[]%s" % (coll_name, "." + attr))
        return getattr(item, attr)

    return _make_proxy(item, get, [a for a in dir(item) if not a.startswith("_")], coll_name + "[]")


def _collection_proxy(real_coll, coll_name):
    deny = DENIED_COLLECTION_METHODS.get(coll_name, frozenset())

    def get(attr):
        if attr.startswith("_"):
            raise AttributeError(attr)
        if attr in deny:
            raise CapabilityDenied("bpy.data.%s.%s" % (coll_name, attr))
        val = getattr(real_coll, attr)
        if callable(val):

            def call(*a, **k):
                out = val(*a, **k)
                if isinstance(out, (int, float, str, bytes, bool, type(None), list, tuple, dict, set)):
                    return out
                return _wrap_datablock(out, coll_name)

            return call
        return val

    return _make_proxy(
        real_coll,
        get,
        [a for a in dir(real_coll) if not a.startswith("_")],
        "bpy.data." + coll_name,
        wrap=lambda item: _wrap_datablock(item, coll_name),
    )


def _data_proxy():
    real = _REAL_BPY.data

    def get(attr):
        if attr in DENIED_DATA_ATTRS:
            raise CapabilityDenied("bpy.data.%s" % attr)
        val = getattr(real, attr)
        if attr in DENIED_COLLECTION_METHODS:
            return _collection_proxy(val, attr)
        return val

    return _make_proxy(real, get, [a for a in dir(real) if not a.startswith("_")], "bpy.data")


def _context_proxy(real):
    def get(attr):
        if attr in DENIED_CONTEXT_ATTRS:
            raise CapabilityDenied("bpy.context.%s" % attr)
        if attr == "temp_override":
            return _TempOverrideFactory(real)
        return getattr(real, attr)

    return _make_proxy(real, get, [a for a in dir(real) if not a.startswith("_")], "bpy.context")


class _TempOverrideFactory:
    __slots__ = ("_real",)

    def __init__(self, real):
        self._real = real

    def __call__(self, **kwargs):
        real = self._real
        blocked = [k for k in kwargs if k in DENIED_CONTEXT_ATTRS]

        class _CM:
            __slots__ = ()

            def __enter__(self):
                if blocked:
                    raise CapabilityDenied("bpy.context.temp_override(%s)" % blocked[0])
                return _context_proxy(real.temp_override(**kwargs).__enter__())

            def __exit__(self, *exc):
                return False

        return _CM()


# ---------------------------------------------------------------------------
# Namespace builder
# ---------------------------------------------------------------------------

_REAL_BPY = None
_REAL_OPS = None


def build_namespace():
    import bpy
    import mathutils  # noqa: F401

    global _REAL_BPY, _REAL_OPS
    _REAL_BPY = bpy
    _REAL_OPS = bpy.ops

    ns = {"__name__": "__main__", "__doc__": None}
    ns["__builtins__"] = {
        k: getattr(real_builtins, k) for k in ALLOWED_BUILTINS if hasattr(real_builtins, k)
    }
    ns["__builtins__"]["__import__"] = guarded_import
    ns["bpy"] = _make_proxy(
        bpy, _bpy_get, sorted(ALLOWED_BPY_ATTRS), "bpy"
    )
    ns["C"] = ns["bpy"].context
    ns["D"] = ns["bpy"].data
    from mathutils import Vector, Matrix, Euler, Quaternion

    ns.update({"Vector": Vector, "Matrix": Matrix, "Euler": Euler, "Quaternion": Quaternion})
    return ns


def run_guarded(code):
    ns = build_namespace()
    try:
        exec(compile(code, "<model>", "exec"), ns, ns)
        return "no-error"
    except CapabilityDenied as e:
        return "denied: " + str(e)
    except BaseException as e:  # noqa: BLE001
        return "%s: %s" % (type(e).__name__, e)


# ---------------------------------------------------------------------------
# BASELINE
# ---------------------------------------------------------------------------


def baseline():
    out = []
    with open(canary("open_baseline"), "w") as fh:
        fh.write("x")
    out.append(("open()", os.path.exists(canary("open_baseline"))))

    real_builtins.__import__("subprocess").run(
        ["/bin/sh", "-c", "touch " + canary("subproc_baseline")]
    )
    out.append(("subprocess", os.path.exists(canary("subproc_baseline"))))

    import bpy

    try:
        bpy.data.libraries.write(canary("lib_baseline") + ".blend", set())
        out.append(("bpy.data.libraries.write", os.path.exists(canary("lib_baseline") + ".blend")))
    except Exception as e:  # noqa: BLE001
        out.append(("bpy.data.libraries.write", "ERR %s" % type(e).__name__))

    img = bpy.data.images.new("probe_base_img", 2, 2)
    img.filepath_raw = canary("img_baseline") + ".png"
    img.file_format = "PNG"
    try:
        img.save()
        out.append(("Image.save", os.path.exists(canary("img_baseline") + ".png")))
    except Exception as e:  # noqa: BLE001
        out.append(("Image.save", "ERR %s" % type(e).__name__))

    txt = bpy.data.texts.new("probe_base_txt")
    txt.from_string("print('baseline')")
    txt.filepath = canary("text_baseline") + ".py"
    try:
        txt.write(text="print('x')")
        out.append(("Text.write", os.path.exists(canary("text_baseline") + ".py")))
    except Exception as e:  # noqa: BLE001
        out.append(("Text.write", "ERR %s: %s" % (type(e).__name__, e)))

    try:
        bpy.ops.wm.save_as_mainfile(filepath=canary("blend_baseline") + ".blend", check_existing=False)
        out.append(("wm.save_as_mainfile", os.path.exists(canary("blend_baseline") + ".blend")))
    except Exception as e:  # noqa: BLE001
        out.append(("wm.save_as_mainfile", "ERR %s" % type(e).__name__))

    n0 = len(bpy.app.handlers.depsgraph_update_post)
    bpy.app.handlers.depsgraph_update_post.append(lambda s: None)
    out.append(("handlers.append", len(bpy.app.handlers.depsgraph_update_post) == n0 + 1))
    bpy.app.handlers.depsgraph_update_post.pop()

    bpy.app.timers.register(lambda: None)
    out.append(("timers.register", True))

    before = bpy.context.preferences.edit.use_global_undo
    bpy.context.preferences.edit.use_global_undo = False
    out.append(("prefs.write", bpy.context.preferences.edit.use_global_undo is False))
    bpy.context.preferences.edit.use_global_undo = before
    return out


# ---------------------------------------------------------------------------
# ATTACKS
# ---------------------------------------------------------------------------

ATTACKS = [
    ("open", "open(%r,'w').write('x')" % canary("open_attack")),
    ("__import__os", "__import__('os')"),
    ("eval", "eval('1+1')"),
    ("exec", "exec('x=1')"),
    ("compile", "compile('1','<x>','eval')"),
    ("breakpoint", "breakpoint()"),
    ("globals", "globals()"),
    ("locals", "locals()"),
    ("import_os", "import os"),
    ("import_subprocess", "import subprocess"),
    ("import_socket", "import socket"),
    ("import_ctypes", "import ctypes"),
    ("import_sys", "import sys"),
    ("import_importlib", "import importlib"),
    ("import_io", "import io"),
    ("import_pathlib", "import pathlib"),
    ("import_gc", "import gc"),
    ("import_inspect", "import inspect"),
    ("import_statistics_ok", "import statistics\nassert statistics.mean([1,2])==1.5"),
    ("import_math_ok", "import math\nassert math.sqrt(4)==2.0"),
    ("import_numpy_ok", "import numpy\nassert numpy.array([1,2]).sum()==3"),
    ("dataclass_ok", "from dataclasses import dataclass\n@dataclass\nclass X:\n    a: int\nassert X(1).a==1"),
    ("enum_ok", "import enum\nclass C(enum.Enum):\n    A=1\nassert C.A.value==1"),
    # module-dict leaks
    ("leak_statistics_sys", "import statistics\nstatistics.sys.modules['os'].system('touch %s')" % canary("leak_stats")),
    ("leak_random_os", "import random\nrandom._os.system('touch %s')" % canary("leak_random")),
    ("leak_uuid_os", "import uuid\nuuid.os.system('touch %s')" % canary("leak_uuid")),
    ("leak_enum_sys", "import enum\nenum.sys.modules['os'].system('touch %s')" % canary("leak_enum")),
    ("leak_dataclasses_inspect", "import dataclasses\ndataclasses.inspect"),
    ("numpy_ctypeslib", "import numpy\nnumpy.ctypeslib"),
    ("numpy_dict", "import numpy\nnumpy.__dict__"),
    # bpy.ops direct and getattr
    ("ops_wm_save", "bpy.ops.wm.save_as_mainfile(filepath=%r)" % (canary("ops_wm") + ".blend")),
    ("ops_wm_getattr", "getattr(bpy.ops.wm,'save_as_mainfile')(filepath=%r)" % (canary("ops_wm2") + ".blend")),
    ("ops_wm_dir", "bpy.ops.wm"),
    ("ops_wm_quit", "bpy.ops.wm.quit_blender()"),
    ("ops_wm_url", "bpy.ops.wm.url_open(url='https://example.com')"),
    ("ops_text_run", "bpy.ops.text.run_script()"),
    ("ops_script_run", "bpy.ops.script.python_file_run(filepath=%r)" % (canary("evil") + ".py")),
    ("ops_image_save", "bpy.ops.image.save_as(filepath=%r)" % (canary("ops_img") + ".png")),
    ("ops_export", "bpy.ops.export_scene.obj(filepath=%r)" % (canary("ops_export") + ".obj")),
    ("ops_ed_undo", "bpy.ops.ed.undo_push(message='x')"),
    ("ops_object_ok", "bpy.ops.mesh.primitive_cube_add()"),
    ("ops_object_poll", "bpy.ops.mesh.primitive_cube_add.get_rna_type()"),
    # bpy.data side door
    ("data_libraries_write", "bpy.data.libraries.write(%r, set())" % (canary("data_lib") + ".blend")),
    ("data_libraries_load", "bpy.data.libraries.load(%r)" % (canary("data_lib") + ".blend")),
    (
        "data_image_save",
        "i=bpy.data.images.new('atk_img',2,2)\ni.filepath_raw=%r\ni.file_format='PNG'\ni.save()"
        % (canary("data_img") + ".png"),
    ),
    ("data_image_load", "bpy.data.images.load('/etc/hosts')"),
    (
        "data_text_write",
        "t=bpy.data.texts.new('atk_txt')\nt.filepath=%r\nt.write(text='x')"
        % (canary("data_text") + ".py"),
    ),
    ("data_image_iter", "n=len([i for i in bpy.data.images])\nprint('imgs',n)"),
    ("data_objects_ok", "o=bpy.data.objects['Cube']\no.location.z=1.0"),
    # bpy.utils / path / props
    ("utils_execfile", "bpy.utils.execfile(%r)" % (canary("evil") + ".py")),
    ("utils_script_paths", "bpy.utils.script_paths()"),
    ("utils_user_resource", "bpy.utils.user_resource('CONFIG')"),
    ("path_os", "bpy.path._os"),
    ("path_dict", "bpy.path.__dict__"),
    ("props_dict", "bpy.props.__dict__"),
    # bpy.app
    ("app_timers", "bpy.app.timers.register(lambda: None)"),
    ("app_handlers", "bpy.app.handlers.depsgraph_update_post.append(lambda s: None)"),
    ("app_driver_ns", "bpy.app.driver_namespace['x']=1"),
    ("app_version_ok", "bpy.app.version_string"),
    # context
    ("ctx_prefs_undo", "bpy.context.preferences.edit.use_global_undo=False"),
    ("ctx_wm", "bpy.context.window_manager"),
    ("ctx_temp_override_prefs", "with bpy.context.temp_override():\n    bpy.context.preferences"),
    ("ctx_temp_override_scene_ok", "with bpy.context.temp_override(scene=bpy.context.scene):\n    bpy.context.scene"),
    ("ctx_scene_ok", "bpy.context.scene.name"),
    # generic introspection - expected OPEN
    ("subclasses_fileio", "().__class__.__bases__[0].__subclasses__()"),
    (
        "subclasses_os",
        "found=None\n"
        "for c in ().__class__.__bases__[0].__subclasses__():\n"
        "    g=getattr(getattr(c,'__init__',None),'__globals__',None)\n"
        "    if g and 'sys' in g:\n"
        "        found=g['sys']; break\n"
        "if found is not None:\n"
        "    os_=found.modules.get('os')\n"
        "    if os_: os_.system('touch %s')\n" % canary("subclasses_os"),
    ),
    (
        "proxy_closure_leak",
        "cl=type(bpy).__getattr__.__closure__\n"
        "real=[c.cell_contents for c in cl][0]\n"
        "real.ops.wm.save_as_mainfile(filepath=%r)" % (canary("closure_wm") + ".blend"),
    ),
    ("proxy_object_getattribute", "object.__getattribute__(bpy,'_real')"),
    ("bpy_dict", "bpy.__dict__"),
    (
        "proxy_globals_leak",
        "g=type(bpy).__getattr__.__globals__\n"
        "rb=g['real_builtins']\n"
        "rb.__import__('os').system('touch %s')\n" % canary("proxy_globals"),
    ),
    ("numpy_save", "import numpy\nnumpy.save(%r, numpy.array([1,2]))" % (canary("numpy_save") + ".npy")),
    ("numpy_load", "import numpy\nnumpy.load('/etc/hosts')"),
    ("numpy_fromfile", "import numpy\nnumpy.fromfile('/etc/hosts', dtype='uint8')"),
    ("numpy_ok_after", "import numpy\nassert numpy.linalg.norm(numpy.array([3.0,4.0]))==5.0"),
    (
        "indirect_screen_text",
        "hits=[]\n"
        "for s in bpy.data.screens:\n"
        "    for a in s.areas:\n"
        "        for sp in a.spaces:\n"
        "            if hasattr(sp,'text'): hits.append(sp)\n"
        "print('screen_text_spaces', len(hits))\n",
    ),
    (
        "indirect_node_image",
        "m=bpy.data.materials['Material']\n"
        "nt=m.node_tree\n"
        "n=nt.nodes.new('ShaderNodeTexImage')\n"
        "n.image=bpy.data.images.new('atk_node_img',2,2)\n"
        "n.image.filepath_raw=%r\n"
        "n.image.file_format='PNG'\n"
        "n.image.save()" % (canary("node_img") + ".png"),
    ),
    (
        "indirect_screen_text_write",
        "t=bpy.data.texts.new('atk_screen_txt')\n"
        "t.filepath=%r\n"
        "for s in bpy.data.screens:\n"
        "    for a in s.areas:\n"
        "        for sp in a.spaces:\n"
        "            if hasattr(sp,'text'):\n"
        "                sp.text=t\n"
        "                sp.text.write(text='x')\n" % (canary("screen_text") + ".py"),
    ),
]


def score(name):
    checks = {
        "open": lambda: os.path.exists(canary("open_attack")),
        "leak_statistics_sys": lambda: os.path.exists(canary("leak_stats")),
        "leak_random_os": lambda: os.path.exists(canary("leak_random")),
        "leak_uuid_os": lambda: os.path.exists(canary("leak_uuid")),
        "leak_enum_sys": lambda: os.path.exists(canary("leak_enum")),
        "ops_wm_save": lambda: os.path.exists(canary("ops_wm") + ".blend"),
        "ops_wm_getattr": lambda: os.path.exists(canary("ops_wm2") + ".blend"),
        "ops_image_save": lambda: os.path.exists(canary("ops_img") + ".png"),
        "ops_export": lambda: os.path.exists(canary("ops_export") + ".obj"),
        "data_libraries_write": lambda: os.path.exists(canary("data_lib") + ".blend"),
        "data_image_save": lambda: os.path.exists(canary("data_img") + ".png"),
        "data_text_write": lambda: os.path.exists(canary("data_text") + ".py"),
        "subclasses_os": lambda: os.path.exists(canary("subclasses_os")),
        "proxy_closure_leak": lambda: os.path.exists(canary("closure_wm") + ".blend"),
        "proxy_globals_leak": lambda: os.path.exists(canary("proxy_globals")),
        "numpy_save": lambda: os.path.exists(canary("numpy_save") + ".npy"),
        "indirect_node_image": lambda: os.path.exists(canary("node_img") + ".png"),
        "indirect_screen_text_write": lambda: os.path.exists(canary("screen_text") + ".py"),
    }
    fn = checks.get(name)
    return None if fn is None else bool(fn())


def attacks():
    out = []
    with open(canary("evil") + ".py", "w") as fh:
        fh.write("open(%r,'w').write('x')\n" % canary("evil_ran"))
    for name, code in ATTACKS:
        err = run_guarded(code)
        escaped = score(name)
        if escaped:
            outcome = "ESCAPED"
        elif err.startswith("denied"):
            outcome = "denied"
        elif err.startswith("AttributeError"):
            outcome = "blocked"
        elif err == "no-error":
            outcome = "allowed"
        else:
            outcome = "blocked"
        out.append({"attack": name, "outcome": outcome, "detail": err[:150]})
    return out


# ---------------------------------------------------------------------------
# INTROSPECTION - escapes the design does NOT claim to close.
# ---------------------------------------------------------------------------


def introspection():
    out = []
    code = (
        "subs=().__class__.__bases__[0].__subclasses__()\n"
        "print('subclass_count', len(subs))\n"
        "for c in subs:\n"
        "    if c.__name__=='FileIO':\n"
        "        f=c(%r,'w'); f.write(b'x'); f.close(); print('FileIO_OK')\n"
        "        break\n"
        "for c in subs:\n"
        "    g=getattr(getattr(c,'__init__',None),'__globals__',None)\n"
        "    if g and 'sys' in g:\n"
        "        sys_=g['sys']\n"
        "        os_=sys_.modules.get('os')\n"
        "        if os_:\n"
        "            os_.system('touch %s')\n"
        "            print('SYS_VIA_GLOBALS_OK', c.__name__)\n"
        "            break\n" % (canary("intro_fileio"), canary("intro_os"))
    )
    ns = build_namespace()
    import io as _io
    from contextlib import redirect_stdout

    buf = _io.StringIO()
    try:
        with redirect_stdout(buf):
            exec(compile(code, "<model>", "exec"), ns, ns)
        err = "no-error"
    except BaseException as e:  # noqa: BLE001
        err = "%s: %s" % (type(e).__name__, e)
    out.append(
        {
            "attempt": "subclasses -> FileIO / sys.modules['os']",
            "stdout": buf.getvalue().strip().splitlines(),
            "error": err[:200],
            "fileio_wrote": os.path.exists(canary("intro_fileio")),
            "os_ran": os.path.exists(canary("intro_os")),
        }
    )
    # closure leak
    code2 = (
        "cl=type(bpy).__getattr__.__closure__\n"
        "real=[c.cell_contents for c in cl][0]\n"
        "print('closure_real_is_bpy', real is not None)\n"
        "print('has_utils', hasattr(real,'utils'))\n"
    )
    ns2 = build_namespace()
    buf2 = _io.StringIO()
    try:
        with redirect_stdout(buf2):
            exec(compile(code2, "<model>", "exec"), ns2, ns2)
        err2 = "no-error"
    except BaseException as e:  # noqa: BLE001
        err2 = "%s: %s" % (type(e).__name__, e)
    out.append(
        {
            "attempt": "type(proxy).__getattr__.__closure__ -> real bpy",
            "stdout": buf2.getvalue().strip().splitlines(),
            "error": err2[:200],
        }
    )
    return out


def main():
    import bpy

    RESULTS["notes"].append("python=" + sys.version.split()[0])
    RESULTS["notes"].append("blender=" + bpy.app.version_string)
    RESULTS["notes"].append("sys.meta_path finder: omitted by design (see comment)")
    RESULTS["baseline"] = [{"attempt": k, "canary_fired": v} for k, v in baseline()]
    RESULTS["attacks"] = attacks()
    RESULTS["introspection"] = introspection()
    print("PROBE_BEGIN")
    print(json.dumps(RESULTS, indent=1, default=str))
    print("PROBE_END")


main()
