from collections import OrderedDict, defaultdict
import subprocess
import copy
import re
import tempfile
import os

import claripy

import logging
l = logging.getLogger("angr.sim_type")

try:
    import pycparser
except ImportError:
    pycparser = None

class SimType(object):
    """
    SimType exists to track type information for SimProcedures.
    """

    _fields = ()
    _arch = None
    _size = None
    _can_refine_int = False
    base = True

    def __init__(self, label=None):
        """
        :param label: the type label.
        """
        self.label = label

    def __eq__(self, other):
        if type(self) != type(other):
            return False

        for attr in self._fields:
            if getattr(self, attr) != getattr(other, attr):
                return False

        return True

    def __ne__(self, other):
        # wow many efficient
        return not self == other

    def __hash__(self):
        # very hashing algorithm many secure wow
        out = hash(type(self))
        for attr in self._fields:
            out ^= hash(getattr(self, attr))
        return out

    def view(self, state, addr):
        return SimMemView(ty=self, addr=addr, state=state)

    @property
    def name(self):
        return repr(self)

    def _refine_dir(self): # pylint: disable=no-self-use
        return []

    def _refine(self, view, k): # pylint: disable=unused-argument,no-self-use
        raise KeyError("{} is not a valid refinement".format(k))

    @property
    def size(self):
        if self._size is not None:
            return self._size
        return NotImplemented

    def with_arch(self, arch):
        if self._arch is not None and self._arch == arch:
            return self
        else:
            return self._with_arch(arch)

    def _with_arch(self, arch):
        cp = copy.copy(self)
        cp._arch = arch
        return cp


class SimTypeBottom(SimType):
    """
    SimTypeBottom basically repesents a type error.
    """

    def __repr__(self):
        return 'BOT'


class SimTypeTop(SimType):
    """
    SimTypeTop represents any type (mostly used with a pointer for void*).
    """

    _fields = ('size',)

    def __init__(self, size=None, label=None):
        SimType.__init__(self, label)
        self._size = size

    def __repr__(self):
        return 'TOP'


class SimTypeReg(SimType):
    """
    SimTypeReg is the base type for all types that are register-sized.
    """

    _fields = ('size',)

    def __init__(self, size, label=None):
        """
        :param label: the type label.
        :param size: the size of the type (e.g. 32bit, 8bit, etc.).
        """
        SimType.__init__(self, label=label)
        self._size = size

    def __repr__(self):
        return "reg{}_t".format(self.size)

    def extract(self, state, addr, concrete=False):
        out = state.memory.load(addr, self.size / 8, endness=state.arch.memory_endness)
        if not concrete:
            return out
        return state.se.any_int(out)

    def store(self, state, addr, value):
        store_endness = state.arch.memory_endness

        if isinstance(value, claripy.ast.Bits):
            if value.size() != self.size:
                raise ValueError("size of expression is wrong size for type")
        elif isinstance(value, (int, long)):
            value = state.se.BVV(value, self.size)
        elif isinstance(value, str):
            store_endness = 'Iend_BE'
        else:
            raise TypeError("unrecognized expression type for SimType {}".format(type(self).__name__))

        state.memory.store(addr, value, endness=store_endness)


class SimTypeNum(SimType):
    """
    SimTypeNum is a numeric type of arbitrary length
    """

    _fields = SimType._fields + ('signed', 'size')

    def __init__(self, size, signed=True, label=None):
        """
        :param size:        The size of the integer, in bytes
        :param signed:      Whether the integer is signed or not
        :param label:       A label for the type
        """
        super(SimTypeNum, self).__init__(label)
        self._size = size
        self.signed = signed

    def __repr__(self):
        return "{}int{}_t".format('' if self.signed else 'u', self.size)

    def extract(self, state, addr, concrete=False):
        out = state.memory.load(addr, self.size / 8, endness=state.arch.memory_endness)
        if not concrete:
            return out
        n = state.se.any_int(out)
        if self.signed and n >= 1 << (self.size-1):
            n -= 1 << (self.size)
        return n

    def store(self, state, addr, value):
        store_endness = state.arch.memory_endness

        if isinstance(value, claripy.ast.Bits):
            if value.size() != self.size:
                raise ValueError("size of expression is wrong size for type")
        elif isinstance(value, (int, long)):
            value = state.se.BVV(value, self.size)
        elif isinstance(value, str):
            store_endness = 'Iend_BE'
        else:
            raise TypeError("unrecognized expression type for SimType {}".format(type(self).__name__))

        state.memory.store(addr, value, endness=store_endness)

class SimTypeInt(SimTypeReg):
    """
    SimTypeInt is a type that specifies a signed or unsigned C integer.
    """

    _fields = SimTypeReg._fields + ('signed',)
    _base_name = 'int'

    def __init__(self, signed=True, label=None):
        """
        :param signed:  True if signed, False if unsigned
        :param label:   The type label
        """
        super(SimTypeInt, self).__init__(None, label=label)
        self.signed = signed

    def __repr__(self):
        name = self._base_name
        if not self.signed:
            name = 'unsigned ' + name

        try:
            return name + ' (%d bits)' % self.size
        except ValueError:
            return name

    @property
    def size(self):
        if self._arch is None:
            raise ValueError("Can't tell my size without an arch!")
        try:
            return self._arch.sizeof[self._base_name]
        except KeyError:
            raise ValueError("Arch %s doesn't have its %s type defined!" % (self._arch.name, self._base_name))

    def extract(self, state, addr, concrete=False):
        out = state.memory.load(addr, self.size / 8, endness=state.arch.memory_endness)
        if not concrete:
            return out
        n = state.se.any_int(out)
        if self.signed and n >= 1 << (self.size-1):
            n -= 1 << (self.size)
        return n


class SimTypeShort(SimTypeInt):
    _base_name = 'short'


class SimTypeLong(SimTypeInt):
    _base_name = 'long'


class SimTypeLongLong(SimTypeInt):
    _base_name = 'long long'


class SimTypeChar(SimTypeReg):
    """
    SimTypeChar is a type that specifies a character;
    this could be represented by an 8-bit int, but this is meant to be interpreted as a character.
    """

    def __init__(self, label=None):
        """
        :param label: the type label.
        """
        SimTypeReg.__init__(self, 8, label=label) # a char better be 8 bits (I'm looking at you, DCPU-16)
        self.signed = False

    def __repr__(self):
        return 'char'

    def store(self, state, addr, value):
        try:
            super(SimTypeChar, self).store(state, addr, value)
        except TypeError:
            if isinstance(value, str) and len(value) == 1:
                value = state.se.BVV(ord(value), 8)
                super(SimTypeChar, self).store(state, addr, value)
            else:
                raise

    def extract(self, state, addr, concrete=False):
        out = super(SimTypeChar, self).extract(state, addr, concrete)
        if concrete:
            return chr(out)
        return out


class SimTypeBool(SimTypeChar):
    def __repr__(self):
        return 'bool'

    def store(self, state, addr, value):
        return super(SimTypeBool, self).store(state, addr, int(value))

    def extract(self, state, addr, concrete=False):
        ver = super(SimTypeBool, self).extract(state, addr, concrete)
        if concrete:
            return ver != '\0'
        return ver != 0


class SimTypeFd(SimTypeReg):
    """
    SimTypeFd is a type that specifies a file descriptor.
    """

    _fields = SimTypeReg._fields

    def __init__(self, label=None):
        """
        :param label: the type label
        """
        # file descriptors are always 32 bits, right?
        super(SimTypeFd, self).__init__(32, label=label)

    def __repr__(self):
        return 'fd_t'

class SimTypePointer(SimTypeReg):
    """
    SimTypePointer is a type that specifies a pointer to some other type.
    """

    _fields = SimTypeReg._fields + ('pts_to',)

    def __init__(self, pts_to, label=None, offset=0):
        """
        :param label:   The type label.
        :param pts_to:  The type to which this pointer points to.
        """
        super(SimTypePointer, self).__init__(None, label=label)
        self.pts_to = pts_to
        self.signed = False
        self.offset = offset

    def __repr__(self):
        return '{}*'.format(self.pts_to)

    def make(self, pts_to):
        new = type(self)(pts_to)
        new._arch = self._arch
        return new

    @property
    def size(self):
        if self._arch is None:
            raise ValueError("Can't tell my size without an arch!")
        return self._arch.bits

    def _with_arch(self, arch):
        out = SimTypePointer(self.pts_to.with_arch(arch), self.label)
        out._arch = arch
        return out


class SimTypeFixedSizeArray(SimType):
    """
    SimTypeFixedSizeArray is a literal (i.e. not a pointer) fixed-size array.
    """

    def __init__(self, elem_type, length):
        super(SimTypeFixedSizeArray, self).__init__()
        self.elem_type = elem_type
        self.length = length

    def __repr__(self):
        return '{}[{}]'.format(self.elem_type, self.length)

    _can_refine_int = True

    def _refine(self, view, k):
        return view._deeper(addr=view._addr + k * (self.elem_type.size/8), ty=self.elem_type)

    def extract(self, state, addr, concrete=False):
        return [self.elem_type.extract(state, addr + i*(self.elem_type.size/8), concrete) for i in xrange(self.length)]

    def store(self, state, addr, values):
        for i, val in enumerate(values):
            self.elem_type.store(state, addr + i*self.elem_type.size, val)

    @property
    def size(self):
        return self.elem_type.size * self.length

    def _with_arch(self, arch):
        out = SimTypeFixedSizeArray(self.elem_type.with_arch(arch), self.length)
        out._arch = arch
        return out


class SimTypeArray(SimType):
    """
    SimTypeArray is a type that specifies a pointer to an array; while it is a pointer, it has a semantic difference.
    """

    _fields = ('elem_type', 'length')

    def __init__(self, elem_type, length=None, label=None):
        """
        :param label:       The type label.
        :param elem_type:   The type of each element in the array.
        :param length:      An expression of the length of the array, if known.
        """
        super(SimTypeArray, self).__init__(label=label)
        self.elem_type = elem_type
        self.length = length

    def __repr__(self):
        return '{}[{}]'.format(self.elem_type, '' if self.length is None else self.length)

    @property
    def size(self):
        if self._arch is None:
            raise ValueError("I can't tell my size without an arch!")
        return self._arch.bits

    def _with_arch(self, arch):
        out = SimTypeArray(self.elem_type.with_arch(arch), self.length, self.label)
        out._arch = arch
        return out


class SimTypeString(SimTypeArray):
    """
    SimTypeString is a type that represents a C-style string,
    i.e. a NUL-terminated array of bytes.
    """

    _fields = SimTypeArray._fields + ('length',)

    def __init__(self, length=None, label=None):
        """
        :param label:   The type label.
        :param length:  An expression of the length of the string, if known.
        """
        super(SimTypeString, self).__init__(SimTypeChar(), label=label, length=length)

    def __repr__(self):
        return 'string_t'

    def extract(self, state, addr, concrete=False):
        if self.length is None:
            out = None
            last_byte = state.memory.load(addr, 1)
            addr += 1
            while not claripy.is_true(last_byte == 0):
                out = last_byte if out is None else out.concat(last_byte)
                last_byte = state.memory.load(addr, 1)
                addr += 1
        else:
            out = state.memory.load(addr, self.length)
        if not concrete:
            return out if out is not None else claripy.BVV(0, 0)
        else:
            return state.se.any_str(out) if out is not None else ''

    _can_refine_int = True

    def _refine(self, view, k):
        return view._deeper(addr=view._addr + k, ty=SimTypeChar())

    @property
    def size(self):
        if self.length is None:
            return 4096         # :/
        return self.length + 1

    def _with_arch(self, arch):
        return self


class SimTypeFunction(SimType):
    """
    SimTypeFunction is a type that specifies an actual function (i.e. not a pointer) with certain types of arguments and
    a certain return value.
    """

    _fields = ('args', 'returnty')
    base = False

    def __init__(self, args, returnty, label=None):
        """
        :param label:   The type label
        :param args:    A tuple of types representing the arguments to the function
        :param returns: The return type of the function, or none for void
        """
        super(SimTypeFunction, self).__init__(label=label)
        self.args = args
        self.returnty = returnty

    def __repr__(self):
        return '({}) -> {}'.format(', '.join(str(a) for a in self.args), self.returnty)

    @property
    def size(self):
        return 4096     # ???????????

    def _with_arch(self, arch):
        out = SimTypeFunction([a.with_arch(arch) for a in self.args], self.returnty.with_arch(arch), self.label)
        out._arch = arch
        return out


class SimTypeLength(SimTypeLong):
    """
    SimTypeLength is a type that specifies the length of some buffer in memory.

    ...I'm not really sure what the original design of this class was going for
    """

    _fields = SimTypeNum._fields + ('addr', 'length') # ?

    def __init__(self, signed=False, addr=None, length=None, label=None):
        """
        :param signed:  Whether the value is signed or not
        :param label:   The type label.
        :param addr:    The memory address (expression).
        :param length:  The length (expression).
        """
        super(SimTypeLength, self).__init__(signed=signed, label=label)
        self.addr = addr
        self.length = length

    def __repr__(self):
        return 'size_t'

    @property
    def size(self):
        if self._arch is None:
            raise ValueError("I can't tell my size without an arch!")
        return self._arch.bits


class SimTypeFloat(SimTypeReg):
    """
    An IEEE754 single-precision floating point number
    """
    def __init__(self, size=32):
        super(SimTypeFloat, self).__init__(size)

    sort = claripy.FSORT_FLOAT
    signed = True

    def extract(self, state, addr, concrete=False):
        itype = claripy.fpToFP(super(SimTypeFloat, self).extract(state, addr, False), self.sort)
        if concrete:
            return state.se.any_int(itype)
        return itype

    def store(self, state, addr, value):
        if type(value) in (int, float, long):
            value = claripy.FPV(float(value), self.sort)
        return super(SimTypeFloat, self).store(state, addr, value)

    def __repr__(self):
        return 'float'


class SimTypeDouble(SimTypeFloat):
    """
    An IEEE754 double-precision floating point number
    """
    def __init__(self):
        super(SimTypeDouble, self).__init__(64)

    sort = claripy.FSORT_DOUBLE

    def __repr__(self):
        return 'double'


class SimStruct(SimType):
    _fields = ('name', 'fields')

    def __init__(self, fields, name=None, pack=True):
        super(SimStruct, self).__init__(None)
        if not pack:
            raise ValueError("you think I've implemented padding, how cute")

        self._name = '<anon>' if name is None else name
        self.fields = fields

    @property
    def name(self): # required bc it's a property in the original
        return self._name

    @property
    def offsets(self):
        offsets = {}
        offset_so_far = 0
        for name, ty in self.fields.iteritems():
            offsets[name] = offset_so_far
            offset_so_far += ty.size / 8

        return offsets

    def extract(self, state, addr, concrete=False):
        values = {}
        for name, offset in self.offsets.iteritems():
            ty = self.fields[name]
            v = ty.view(state, addr + offset)
            if concrete:
                values[name] = v.concrete
            else:
                values[name] = v

        return SimStructValue(self, values=values)

    def _with_arch(self, arch):
        out = SimStruct(OrderedDict((k, v.with_arch(arch)) for k, v in self.fields.iteritems()), self.name, True)
        out._arch = arch
        return out

    def __repr__(self):
        return 'struct %s' % self.name

    @property
    def size(self):
        return sum(val.size for val in self.fields.itervalues())

    def _refine_dir(self):
        return self.fields.keys()

    def _refine(self, view, k):
        offset = self.offsets[k]
        ty = self.fields[k]
        return view._deeper(ty=ty, addr=view._addr + offset)


class SimStructValue(object):
    """
    A SimStruct type paired with some real values
    """
    def __init__(self, struct, values=None):
        """
        :param struct:      A SimStruct instance describing the type of this struct
        :param values:      A mapping from struct fields to values
        """
        self._struct = struct
        self._values = defaultdict(lambda: None, values or ())

    def __repr__(self):
        fields = ('.{} = {}'.format(name, self._values[name]) for name in self._struct.fields)
        return '{{\n  {}\n}}'.format(',\n  '.join(fields))

class SimUnion(SimType):
    """
    why
    """
    def __init__(self, members, label=None):
        """
        :param members:     The members of the struct, as a mapping name -> type
        """
        super(SimUnion, self).__init__(label)
        self.members = members

    @property
    def size(self):
        return max(ty.size for ty in self.members.itervalues())

    def __repr__(self):
        return 'union {\n\t%s\n}' % '\n\t'.join('%s %s;' % (name, repr(ty)) for name, ty in self.members.iteritems())

    def _with_arch(self, arch):
        out = SimUnion({name: ty.with_arch(arch) for name, ty in self.members.iteritems()}, self.label)
        out._arch = arch
        return out

BASIC_TYPES = {
    'char': SimTypeChar(),
    'signed char': SimTypeChar(),
    'unsigned char': SimTypeChar(),

    'short': SimTypeShort(True),
    'signed short': SimTypeShort(True),
    'unsigned short': SimTypeShort(False),
    'short int': SimTypeShort(True),
    'signed short int': SimTypeShort(True),
    'unsigned short int': SimTypeShort(False),

    'int': SimTypeInt(True),
    'signed int': SimTypeInt(True),
    'unsigned int': SimTypeInt(False),

    'long': SimTypeLong(True),
    'signed long': SimTypeLong(True),
    'unsigned long': SimTypeLong(False),
    'long int': SimTypeLong(True),
    'signed long int': SimTypeLong(True),
    'unsigned long int': SimTypeLong(False),

    'long long': SimTypeLongLong(True),
    'signed long long': SimTypeLongLong(True),
    'unsigned long long': SimTypeLongLong(False),
    'long long int': SimTypeLongLong(True),
    'signed long long int': SimTypeLongLong(True),
    'unsigned long long int': SimTypeLongLong(False),

    'float': SimTypeFloat(),
    'double': SimTypeDouble(),
    'void': SimTypeBottom(),
}

ALL_TYPES = {
    'int8_t': SimTypeNum(8, True),
    'uint8_t': SimTypeNum(8, False),
    'byte': SimTypeNum(8, False),

    'int16_t': SimTypeNum(16, True),
    'uint16_t': SimTypeNum(16, False),
    'word': SimTypeNum(16, False),

    'int32_t': SimTypeNum(32, True),
    'uint32_t': SimTypeNum(32, False),
    'dword': SimTypeNum(32, False),

    'int64_t': SimTypeNum(64, True),
    'uint64_t': SimTypeNum(64, False),
    'qword': SimTypeNum(64, False),

    'ptrdiff_t': SimTypeLong(False),
    'size_t': SimTypeLength(False),
    'ssize_t': SimTypeLength(True),
    'uintptr_t' : SimTypeLong(False),

    'string': SimTypeString(),
}

ALL_TYPES.update(BASIC_TYPES)

# this is a hack, pending https://github.com/eliben/pycparser/issues/187
def make_preamble():
    out = []
    for ty in ALL_TYPES:
        if ty in BASIC_TYPES:
            continue
        if ' ' in ty:
            continue

        typ = ALL_TYPES[ty]
        if isinstance(typ, (SimTypeFunction, SimTypeString)):
            continue

        if isinstance(typ, (SimTypeNum, SimTypeInt)) and str(typ) not in BASIC_TYPES:
            try:
                styp = {8: 'char', 16: 'short', 32: 'int', 64: 'long long'}[typ._size]
            except KeyError:
                styp = 'long' # :(
            if not typ.signed:
                styp = 'unsigned ' + styp
            typ = styp

        out.append('typedef %s %s;' % (typ, ty))

    return '\n'.join(out) + '\n'

def define_struct(defn):
    """
    Register a struct definition globally

    >>> define_struct('struct abcd {int x; int y;}')
    """
    struct = parse_type(defn)
    ALL_TYPES[struct.name] = struct
    return struct

def register_types(mapping):
    """
    Pass in a mapping from name to SimType and they will be registered to the global type store

    >>> register_types(parse_types("typedef int x; typedef float y;"))
    """
    ALL_TYPES.update(mapping)

def do_preprocess(defn):
    """
    Run a string through the C preprocessor that ships with pycparser but is weirdly inaccessable?
    """
    import pycparser.ply.lex as lex
    import pycparser.ply.cpp as cpp
    lexer = lex.lex(cpp)
    p = cpp.Preprocessor(lexer)
    # p.add_path(dir) will add dir to the include search path
    p.parse(defn)
    return ''.join(tok.value for tok in p.parser if tok.type not in p.ignore)

def parse_defns(defn, preprocess=True):
    """
    Parse a series of C definitions, returns a mapping from variable name to variable type object
    """
    return parse_file(defn, preprocess=preprocess)[0]

def parse_types(defn, preprocess=True):
    """
    Parse a series of C definitions, returns a mapping from type name to type object
    """
    return parse_file(defn, preprocess=preprocess)[1]

_include_re = re.compile(r'^\s*#include')
def parse_file(defn, preprocess=True):
    """
    Parse a series of C definitions, returns a tuple of two type mappings, one for variable
    definitions and one for type definitions.
    """
    if pycparser is None:
        raise ImportError("Please install pycparser in order to parse C definitions")

    defn = '\n'.join(x for x in defn.split('\n') if _include_re.match(x) is None)

    if preprocess:
        defn = do_preprocess(defn)

    node = pycparser.c_parser.CParser().parse(make_preamble() + defn)
    if not isinstance(node, pycparser.c_ast.FileAST):
        raise ValueError("Something went horribly wrong using pycparser")
    out = {}
    extra_types = {}
    for piece in node.ext:
        if isinstance(piece, pycparser.c_ast.FuncDef):
            out[piece.decl.name] = _decl_to_type(piece.decl.type, extra_types)
        elif isinstance(piece, pycparser.c_ast.Decl):
            ty = _decl_to_type(piece.type, extra_types)
            if piece.name is not None:
                out[piece.name] = ty
        elif isinstance(piece, pycparser.c_ast.Typedef):
            extra_types[piece.name] = _decl_to_type(piece.type, extra_types)

    return out, extra_types


def parse_type(defn, preprocess=True):
    """
    Parse a simple type expression into a SimType

    >>> parse_type('int *')
    """
    if pycparser is None:
        raise ImportError("Please install pycparser in order to parse C definitions")

    defn = 'typedef ' + defn.strip('; \n\t\r') + ' QQQQ;'

    if preprocess:
        defn = do_preprocess(defn)

    node = pycparser.c_parser.CParser().parse(make_preamble() + defn)
    if not isinstance(node, pycparser.c_ast.FileAST) or \
            not isinstance(node.ext[-1], pycparser.c_ast.Typedef):
        raise ValueError("Something went horribly wrong using pycparser")

    decl = node.ext[-1].type
    return _decl_to_type(decl)

def _decl_to_type(decl, extra_types=None):
    if extra_types is None: extra_types = {}

    if isinstance(decl, pycparser.c_ast.FuncDecl):
        argtyps = () if decl.args is None else [_decl_to_type(x.type, extra_types) for x in decl.args.params]
        return SimTypeFunction(argtyps, _decl_to_type(decl.type, extra_types))

    elif isinstance(decl, pycparser.c_ast.TypeDecl):
        return _decl_to_type(decl.type, extra_types)

    elif isinstance(decl, pycparser.c_ast.PtrDecl):
        pts_to = _decl_to_type(decl.type, extra_types)
        return SimTypePointer(pts_to)

    elif isinstance(decl, pycparser.c_ast.ArrayDecl):
        elem_type = _decl_to_type(decl.type, extra_types)
        try:
            size = _parse_const(decl.dim)
        except ValueError as e:
            l.warning("Got error parsing array dimension, defaulting to zero: %s", e)
            size = 0
        return SimTypeFixedSizeArray(elem_type, size)

    elif isinstance(decl, pycparser.c_ast.Struct):
        struct = SimStruct(OrderedDict(), decl.name)
        if decl.name is not None:
            key = 'struct ' + decl.name
            if key in extra_types:
                struct = extra_types[key]
            else:
                extra_types[key] = struct

        if decl.decls is not None:
            for field in decl.decls:
                struct.fields[field.name] = _decl_to_type(field.type, extra_types)
        return struct

    elif isinstance(decl, pycparser.c_ast.Union):
        members = {child[1].name: _decl_to_type(child[1].type, extra_types) for child in decl.children()}
        return SimUnion(members)

    elif isinstance(decl, pycparser.c_ast.IdentifierType):
        key = ' '.join(decl.names)
        if key in extra_types:
            return extra_types[key]
        elif key in ALL_TYPES:
            return ALL_TYPES[key]
        else:
            raise TypeError("Unknown type '%s'" % ' '.join(key))

    raise ValueError("Unknown type!")

def _parse_const(c):
    if type(c) is pycparser.c_ast.Constant:
        return int(c.value)
    elif type(c) is pycparser.c_ast.BinaryOp:
        if c.op == '+':
            return _parse_const(c.children()[0][1]) + _parse_const(c.children()[1][1])
        if c.op == '-':
            return _parse_const(c.children()[0][1]) - _parse_const(c.children()[1][1])
        if c.op == '*':
            return _parse_const(c.children()[0][1]) * _parse_const(c.children()[1][1])
        if c.op == '/':
            return _parse_const(c.children()[0][1]) // _parse_const(c.children()[1][1])
        raise ValueError('Binary op %s' % c.op)
    else:
        raise ValueError(c)

try:
    define_struct("""
struct example {
    int foo;
    int bar;
    char *hello;
};
""")
except ImportError:
    pass


from .state_plugins.view import SimMemView
