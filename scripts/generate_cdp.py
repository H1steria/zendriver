#!/usr/bin/env -S uv run
import builtins
import itertools
import json
import logging
import operator
import os
import re
import subprocess
import typing
import urllib.request
import shutil
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from textwrap import dedent
from textwrap import indent as tw_indent

import inflection

log_level = getattr(logging, os.environ.get("LOG_LEVEL", "info").upper())
logging.basicConfig(level=log_level)
logger = logging.getLogger("cdpgen")

REPO_ROOT = Path(__file__).parent.parent

GENERATED_PACKAGE_NOTICE = """\
## Generated by PyCDP

The modules of this package were generated by [pycdp], do not modify their contents because the
changes will be overwritten in next generations.
"""

SHARED_HEADER = """# DO NOT EDIT THIS FILE!
#
# This file is generated from the CDP specification. If you need to make
# changes, edit the generator and regenerate all of the modules."""

INIT_HEADER = """{}

""".format(SHARED_HEADER)

MODULE_HEADER = """{}
#
# CDP domain: {{}}{{}}

from __future__ import annotations
import enum
import typing
from dataclasses import dataclass
from .util import event_class, T_JSON_DICT

""".format(SHARED_HEADER)

current_version = ""

BACKTICK_RE = re.compile(r"`([^`]+)`(\w+)?")


def indent(s: str, n: int) -> str:
    """A shortcut for ``textwrap.indent`` that always uses spaces."""
    return tw_indent(s, n * " ")


def escape_backticks(docstr: str) -> str:
    """
    Escape backticks in a docstring by doubling them up.

    This is a little tricky because RST requires a non-letter character after
    the closing backticks, but some CDPs docs have things like "`AxNodeId`s".
    If we double the backticks in that string, then it won't be valid RST. The
    fix is to insert an apostrophe if an "s" trails the backticks.
    """

    def replace_one(match: re.Match[str]) -> str:
        if match.group(2) == "s":
            return f"``{match.group(1)}``'s"
        elif match.group(2):
            # This case (some trailer other than "s") doesn't currently exist
            # in the CDP definitions, but it's here just to be safe.
            return f"``{match.group(1)}`` {match.group(2)}"
        else:
            return f"``{match.group(1)}``"

    # Sometimes pipes are used where backticks should have been used.
    docstr = docstr.replace("|", "`")
    return BACKTICK_RE.sub(replace_one, docstr)


def inline_doc(description: str | None) -> str:
    """Generate an inline doc, e.g. ``#: This type is a ...``"""
    if not description:
        return ""

    description = escape_backticks(description)
    lines = ["#: {}".format(line) for line in description.split("\n")]
    return "\n".join(lines)


def docstring(description: typing.Optional[str]) -> str:
    """Generate a docstring from a description."""
    if not description:
        return ""
    # if original description uses escape sequences it should be generated as a raw docstring
    description = escape_backticks(description)
    if "\\" in description:
        return dedent("r'''\n{}\n'''").format(description)
    else:
        return dedent("'''\n{}\n'''").format(description)


def is_builtin(name: str) -> bool:
    """Return True if ``name`` would shadow a builtin."""
    try:
        getattr(builtins, name)
        return True
    except AttributeError:
        return False


def snake_case(name: str) -> str:
    """Convert a camel case name to snake case. If the name would shadow a
    Python builtin, then append an underscore."""
    name = inflection.underscore(name)
    if is_builtin(name):
        name += "_"
    return name


def ref_to_python(ref: str) -> str:
    """
    Convert a CDP ``$ref`` to the name of a Python type.

    For a dotted ref, the part before the dot is snake cased.
    """
    if "." in ref:
        domain, subtype = ref.split(".")
        ref = "{}.{}".format(snake_case(domain), subtype)
    return f"{ref}"


def ref_to_python_domain(ref: str, domain: str) -> str:
    if ref.startswith(domain + "."):
        return ref_to_python(ref[len(domain) + 1 :])
    return ref_to_python(ref)


class CdpPrimitiveType(Enum):
    """All of the CDP types that map directly to a Python type."""

    boolean = "bool"
    integer = "int"
    number = "float"
    object = "dict"
    string = "str"

    @classmethod
    def get_annotation(cls, cdp_type: str | None) -> str:
        """Return a type annotation for the CDP type."""
        if not cdp_type or cdp_type == "any":
            return "typing.Any"
        else:
            return cls[cdp_type].value

    @classmethod
    def get_constructor(cls, cdp_type: str | None, val: str) -> str:
        """Return the code to construct a value for a given CDP type."""
        if not cdp_type or cdp_type == "any":
            return val
        else:
            cons = cls[cdp_type].value
            return f"{cons}({val})"


@dataclass
class CdpItems:
    """Represents the type of a repeated item."""

    type: str
    ref: str

    @classmethod
    def from_json(cls, type: dict[str, str]) -> "CdpItems":
        """Generate code to instantiate an item from a JSON object."""
        type_ = type.get("type", "any")
        ref = type.get("$ref")
        if not type_ or not ref:
            raise ValueError(
                "CdpItems must have a 'type' and '$ref' field: {}".format(type)
            )
        return cls(type_, ref)


@dataclass
class CdpProperty:
    """A property belonging to a non-primitive CDP type."""

    name: str
    description: typing.Optional[str]
    type: typing.Optional[str]
    ref: typing.Optional[str]
    enum: typing.List[str]
    items: typing.Optional[CdpItems]
    optional: bool
    experimental: bool
    deprecated: bool
    domain: str

    @property
    def py_name(self) -> str:
        """Get this property's Python name."""
        return snake_case(self.name)

    @property
    def py_annotation(self) -> str:
        """This property's Python type annotation."""
        if self.items:
            if self.items.ref:
                py_ref = ref_to_python_domain(self.items.ref, self.domain)
                ann = "typing.List[{}]".format(py_ref)
            else:
                ann = "typing.List[{}]".format(
                    CdpPrimitiveType.get_annotation(self.items.type)
                )
        else:
            if self.ref:
                py_ref = ref_to_python_domain(self.ref, self.domain)
                ann = py_ref
            else:
                ann = CdpPrimitiveType.get_annotation(typing.cast(str, self.type))
        if self.optional:
            ann = f"typing.Optional[{ann}]"
        return ann

    @classmethod
    def from_json(cls, prop: dict[str, typing.Any], domain: str) -> "CdpProperty":
        """Instantiate a CDP property from a JSON object."""
        return cls(
            prop["name"],
            prop.get("description"),
            prop.get("type"),
            prop.get("$ref"),
            prop.get("enum", []),
            CdpItems.from_json(prop["items"]) if "items" in prop else None,
            prop.get("optional", False),
            prop.get("experimental", False),
            prop.get("deprecated", False),
            domain,
        )

    def generate_decl(self) -> str:
        """Generate the code that declares this property."""
        code = inline_doc(self.description)
        if code:
            code += "\n"
        code += f"{self.py_name}: {self.py_annotation}"
        if self.optional:
            code += " = None"
        return code

    def generate_to_json(self, dict_: str, use_self: bool = True) -> str:
        """Generate the code that exports this property to the specified JSON
        dict."""
        self_ref = "self." if use_self else ""
        assign = f"{dict_}['{self.name}'] = "
        if self.items:
            if self.items.ref:
                assign += f"[i.to_json() for i in {self_ref}{self.py_name}]"
            else:
                assign += f"[i for i in {self_ref}{self.py_name}]"
        else:
            if self.ref:
                assign += f"{self_ref}{self.py_name}.to_json()"
            else:
                assign += f"{self_ref}{self.py_name}"
        if self.optional:
            code = dedent(
                f"""\
                if {self_ref}{self.py_name} is not None:
                    {assign}"""
            )
        else:
            code = assign
        return code

    def generate_from_json(self, dict_: str) -> str:
        """Generate the code that creates an instance from a JSON dict named
        ``dict_``."""
        if self.items:
            if self.items.ref:
                py_ref = ref_to_python_domain(self.items.ref, self.domain)
                expr = f"[{py_ref}.from_json(i) for i in {dict_}['{self.name}']]"
            else:
                cons = CdpPrimitiveType.get_constructor(self.items.type, "i")
                expr = f"[{cons} for i in {dict_}['{self.name}']]"
        else:
            if self.ref:
                py_ref = ref_to_python_domain(self.ref, self.domain)
                expr = f"{py_ref}.from_json({dict_}['{self.name}'])"
            else:
                expr = CdpPrimitiveType.get_constructor(
                    self.type, f"{dict_}['{self.name}']"
                )
        if self.optional:
            expr = f"{expr} if {dict_}.get('{self.name}', None) is not None else None"
        return expr


@dataclass
class CdpType:
    """A top-level CDP type."""

    id: str
    description: typing.Optional[str]
    type: str
    items: typing.Optional[CdpItems]
    enum: typing.List[str]
    properties: typing.List[CdpProperty]

    @classmethod
    def from_json(cls, type_: dict[str, typing.Any], domain: str) -> "CdpType":
        """Instantiate a CDP type from a JSON object."""
        return cls(
            type_["id"],
            type_.get("description"),
            type_["type"],
            CdpItems.from_json(type_["items"]) if "items" in type_ else None,
            type_.get("enum", []),
            [CdpProperty.from_json(p, domain) for p in type_.get("properties", list())],
        )

    def generate_code(self) -> str:
        """Generate Python code for this type."""
        logger.debug("Generating type %s: %s", self.id, self.type)
        if self.enum:
            return self.generate_enum_code()
        elif self.properties:
            return self.generate_class_code()
        else:
            return self.generate_primitive_code()

    def generate_primitive_code(self) -> str:
        """Generate code for a primitive type."""
        if self.items:
            if self.items.ref:
                nested_type = ref_to_python(self.items.ref)
            else:
                nested_type = CdpPrimitiveType.get_annotation(self.items.type)
            py_type = f"typing.List[{nested_type}]"
            superclass = "list"
        else:
            # A primitive type cannot have a ref, so there is no branch here.
            py_type = CdpPrimitiveType.get_annotation(self.type)
            superclass = py_type

        code = f"class {self.id}({superclass}):\n"
        doc = docstring(self.description)
        if doc:
            code += indent(doc, 4) + "\n"

        def_to_json = dedent(
            f"""\
            def to_json(self) -> {py_type}:
                return self"""
        )
        code += indent(def_to_json, 4)

        def_from_json = dedent(
            f"""\
            @classmethod
            def from_json(cls, json: {py_type}) -> {self.id}:
                return cls(json)"""
        )
        code += "\n\n" + indent(def_from_json, 4)

        def_repr = dedent(
            f"""\
            def __repr__(self):
                return '{self.id}({{}})'.format(super().__repr__())"""
        )
        code += "\n\n" + indent(def_repr, 4)

        return code

    def generate_enum_code(self) -> str:
        """
        Generate an "enum" type.

        Enums are handled by making a python class that contains only class
        members. Each class member is upper snaked case, e.g.
        ``MyTypeClass.MY_ENUM_VALUE`` and is assigned a string value from the
        CDP metadata.
        """
        def_to_json = dedent(
            """\
            def to_json(self) -> str:
                return self.value"""
        )

        def_from_json = dedent(
            f"""\
            @classmethod
            def from_json(cls, json: str) -> {self.id}:
                return cls(json)"""
        )

        code = f"class {self.id}(enum.Enum):\n"
        doc = docstring(self.description)
        if doc:
            code += indent(doc, 4) + "\n"
        for enum_member in self.enum:
            snake_name = snake_case(enum_member).upper()
            enum_code = f'{snake_name} = "{enum_member}"\n'
            code += indent(enum_code, 4)
        code += "\n" + indent(def_to_json, 4)
        code += "\n\n" + indent(def_from_json, 4)

        return code

    def generate_class_code(self) -> str:
        """
        Generate a class type.

        Top-level types that are defined as a CDP ``object`` are turned into Python
        dataclasses.
        """
        # children = set()
        code = dedent(
            f"""\
            @dataclass
            class {self.id}:\n"""
        )
        doc = docstring(self.description)
        if doc:
            code += indent(doc, 4) + "\n"

        # Emit property declarations. These are sorted so that optional
        # properties come after required properties, which is required to make
        # the dataclass constructor work.
        props = list(self.properties)
        props.sort(key=operator.attrgetter("optional"))
        code += "\n\n".join(indent(p.generate_decl(), 4) for p in props)
        code += "\n\n"

        # Emit to_json() method. The properties are sorted in the same order as
        # above for readability.
        def_to_json = dedent(
            """\
            def to_json(self) -> T_JSON_DICT:
                json: T_JSON_DICT = dict()
        """
        )
        assigns = (p.generate_to_json(dict_="json") for p in props)
        def_to_json += indent("\n".join(assigns), 4)
        def_to_json += "\n"
        def_to_json += indent("return json", 4)
        code += indent(def_to_json, 4) + "\n\n"

        # Emit from_json() method. The properties are sorted in the same order
        # as above for readability.
        def_from_json = dedent(
            f"""\
            @classmethod
            def from_json(cls, json: T_JSON_DICT) -> {self.id}:
                return cls(
        """
        )
        from_jsons = list()
        for p in props:
            from_json = p.generate_from_json(dict_="json")
            from_jsons.append(f"{p.py_name}={from_json},")
        def_from_json += indent("\n".join(from_jsons), 8)
        def_from_json += "\n"
        def_from_json += indent(")", 4)
        code += indent(def_from_json, 4)

        return code

    def get_refs(self) -> typing.Set[str]:
        """Return all refs for this type."""
        refs = set()
        if self.enum:
            # Enum types don't have refs.
            pass
        elif self.properties:
            # Enumerate refs for a class type.
            for prop in self.properties:
                if prop.items and prop.items.ref:
                    refs.add(prop.items.ref)
                elif prop.ref:
                    refs.add(prop.ref)
        else:
            # A primitive type can't have a direct ref, but it can have an items
            # which contains a ref.
            if self.items and self.items.ref:
                refs.add(self.items.ref)
        return refs


class CdpParameter(CdpProperty):
    """A parameter to a CDP command."""

    def generate_code(self) -> str:
        """Generate the code for a parameter in a function call."""
        if self.items:
            if self.items.ref:
                nested_type = ref_to_python(self.items.ref)
                py_type = f"typing.List[{nested_type}]"
            else:
                nested_type = CdpPrimitiveType.get_annotation(self.items.type)
                py_type = f"typing.List[{nested_type}]"
        else:
            if self.ref:
                py_type = "{}".format(ref_to_python(self.ref))
            else:
                py_type = CdpPrimitiveType.get_annotation(typing.cast(str, self.type))
        if self.optional:
            py_type = f"typing.Optional[{py_type}]"
        code = f"{self.py_name}: {py_type}"
        if self.optional:
            code += " = None"
        return code

    def generate_decl(self) -> str:
        """Generate the declaration for this parameter."""
        if self.description:
            code = inline_doc(self.description)
            code += "\n"
        else:
            code = ""
        code += f"{self.py_name}: {self.py_annotation}"
        return code

    def generate_doc(self) -> str:
        """Generate the docstring for this parameter."""
        doc = f":param {self.py_name}:"

        if self.deprecated:
            doc += " **(DEPRECATED)**"

        if self.experimental:
            doc += " **(EXPERIMENTAL)**"

        if self.optional:
            doc += " *(Optional)*"

        if self.description:
            desc = self.description.replace("`", "``").replace("\n", " ")
            doc += f" {desc}"
        return doc

    def generate_from_json(self, dict_: str) -> str:
        """
        Generate the code to instantiate this parameter from a JSON dict.
        """
        code = super().generate_from_json(dict_)
        return f"{self.py_name}={code}"


class CdpReturn(CdpProperty):
    """A return value from a CDP command."""

    @property
    def py_annotation(self) -> str:
        """Return the Python type annotation for this return."""
        if self.items:
            if self.items.ref:
                py_ref = ref_to_python(self.items.ref)
                ann = f"typing.List[{py_ref}]"
            else:
                py_type = CdpPrimitiveType.get_annotation(self.items.type)
                ann = f"typing.List[{py_type}]"
        else:
            if self.ref:
                py_ref = ref_to_python(self.ref)
                ann = f"{py_ref}"
            else:
                ann = CdpPrimitiveType.get_annotation(self.type)
        if self.optional:
            ann = f"typing.Optional[{ann}]"
        return ann

    def generate_doc(self) -> str:
        """Generate the docstring for this return."""
        if self.description:
            doc = self.description.replace("\n", " ")
            if self.optional:
                doc = f"*(Optional)* {doc}"
        else:
            doc = ""
        return doc

    def generate_return(self, dict_: str) -> str:
        """Generate code for returning this value."""
        return super().generate_from_json(dict_)


@dataclass
class CdpCommand:
    """A CDP command."""

    name: str
    description: typing.Optional[str]
    experimental: bool
    deprecated: bool
    parameters: typing.List[CdpParameter]
    returns: typing.List[CdpReturn]
    domain: str

    @property
    def py_name(self) -> str:
        """Get a Python name for this command."""
        return snake_case(self.name)

    @classmethod
    def from_json(cls, command: dict[str, typing.Any], domain: str) -> "CdpCommand":
        """Instantiate a CDP command from a JSON object."""
        parameters = command.get("parameters", list())
        returns = command.get("returns", list())

        return cls(
            command["name"],
            command.get("description"),
            command.get("experimental", False),
            command.get("deprecated", False),
            [
                typing.cast(CdpParameter, CdpParameter.from_json(p, domain))
                for p in parameters
            ],
            [typing.cast(CdpReturn, CdpReturn.from_json(r, domain)) for r in returns],
            domain,
        )

    def generate_code(self) -> str:
        """Generate code for a CDP command."""
        global current_version
        # Generate the function header
        if len(self.returns) == 0:
            ret_type = "None"
        elif len(self.returns) == 1:
            ret_type = self.returns[0].py_annotation
        else:
            nested_types = ", ".join(r.py_annotation for r in self.returns)
            ret_type = f"typing.Tuple[{nested_types}]"
        ret_type = f"typing.Generator[T_JSON_DICT,T_JSON_DICT,{ret_type}]"

        code = ""

        if self.deprecated:
            code += f'@deprecated(version="{current_version}")\n'

        code += f"def {self.py_name}("
        ret = f") -> {ret_type}:\n"
        if self.parameters:
            sorted_params = sorted(
                self.parameters, key=lambda param: 1 if param.optional else 0
            )
            code += "\n"
            code += indent(",\n".join(p.generate_code() for p in sorted_params), 8)
            code += "\n"
            code += indent(ret, 4)
        else:
            code += ret

        # Generate the docstring
        doc = ""
        if self.description:
            doc = self.description
        if self.deprecated:
            doc += f"\n\n.. deprecated:: {current_version}"
        if self.experimental:
            doc += "\n\n**EXPERIMENTAL**"
        if self.parameters and doc:
            doc += "\n\n"
        elif not self.parameters and self.returns:
            doc += "\n"
        doc += "\n".join(p.generate_doc() for p in self.parameters)
        if len(self.returns) == 1:
            doc += "\n"
            ret_doc = self.returns[0].generate_doc()
            doc += f":returns: {ret_doc}"
        elif len(self.returns) > 1:
            doc += "\n"
            doc += ":returns: A tuple with the following items:\n\n"
            ret_docs = "\n".join(
                f"{i}. **{r.name}** - {r.generate_doc()}"
                for i, r in enumerate(self.returns)
            )
            doc += indent(ret_docs, 4)
        if doc:
            code += indent(docstring(doc), 4)

        # Generate the function body
        if self.parameters:
            code += "\n"
            code += indent("params: T_JSON_DICT = dict()", 4)
            code += "\n"
        assigns = (
            p.generate_to_json(dict_="params", use_self=False) for p in self.parameters
        )
        code += indent("\n".join(assigns), 4)
        code += "\n"
        code += indent("cmd_dict: T_JSON_DICT = {\n", 4)
        code += indent(f"'method': '{self.domain}.{self.name}',\n", 8)
        if self.parameters:
            code += indent("'params': params,\n", 8)
        code += indent("}\n", 4)
        code += indent("json = yield cmd_dict", 4)
        if len(self.returns) == 0:
            pass
        elif len(self.returns) == 1:
            ret = self.returns[0].generate_return(dict_="json")
            code += indent(f"\nreturn {ret}", 4)
        else:
            ret = "\nreturn (\n"
            expr = ",\n".join(r.generate_return(dict_="json") for r in self.returns)
            ret += indent(expr, 4)
            ret += "\n)"
            code += indent(ret, 4)
        return code

    def get_refs(self) -> typing.Set[str]:
        """Get all refs for this command."""
        refs = set()
        for type_ in itertools.chain(self.parameters, self.returns):
            if type_.items and type_.items.ref:
                refs.add(type_.items.ref)
            elif type_.ref:
                refs.add(type_.ref)
        return refs


@dataclass
class CdpEvent:
    """A CDP event object."""

    name: str
    description: typing.Optional[str]
    deprecated: bool
    experimental: bool
    parameters: typing.List[CdpParameter]
    domain: str

    @property
    def py_name(self) -> str:
        """Return the Python class name for this event."""
        return inflection.camelize(self.name, uppercase_first_letter=True)

    @classmethod
    def from_json(cls, json: dict[str, typing.Any], domain: str) -> "CdpEvent":
        """Create a new CDP event instance from a JSON dict."""
        return cls(
            json["name"],
            json.get("description"),
            json.get("deprecated", False),
            json.get("experimental", False),
            [
                typing.cast(CdpParameter, CdpParameter.from_json(p, domain))
                for p in json.get("parameters", list())
            ],
            domain,
        )

    def generate_code(self) -> str:
        """Generate code for a CDP event."""
        global current_version
        code = dedent(
            f"""\
            @event_class('{self.domain}.{self.name}')
            @dataclass
            class {self.py_name}:"""
        )

        if self.deprecated:
            code = f'@deprecated(version="{current_version}")\n' + code

        code += "\n"
        desc = ""
        if self.description or self.experimental:
            if self.experimental:
                desc += "**EXPERIMENTAL**\n\n"

            if self.description:
                desc += self.description

            code += indent(docstring(desc), 4)
            code += "\n"
        code += indent("\n".join(p.generate_decl() for p in self.parameters), 4)
        code += "\n\n"
        def_from_json = dedent(
            f"""\
            @classmethod
            def from_json(cls, json: T_JSON_DICT) -> {self.py_name}:
                return cls(
        """
        )
        code += indent(def_from_json, 4)
        from_json = ",\n".join(
            p.generate_from_json(dict_="json") for p in self.parameters
        )
        code += indent(from_json, 12)
        code += "\n"
        code += indent(")", 8)
        return code

    def get_refs(self) -> typing.Set[str]:
        """Get all refs for this event."""
        refs = set()
        for param in self.parameters:
            if param.items and param.items.ref:
                refs.add(param.items.ref)
            elif param.ref:
                refs.add(param.ref)
        return refs


@dataclass
class CdpDomain:
    """A CDP domain contains metadata, types, commands, and events."""

    domain: str
    description: typing.Optional[str]
    experimental: bool
    dependencies: typing.List[str]
    types: typing.List[CdpType]
    commands: typing.List[CdpCommand]
    events: typing.List[CdpEvent]

    @property
    def module(self) -> str:
        """The name of the Python module for this CDP domain."""
        return snake_case(self.domain)

    @classmethod
    def from_json(cls, domain: dict[str, typing.Any]) -> "CdpDomain":
        """Instantiate a CDP domain from a JSON object."""
        types = domain.get("types", list())
        commands = domain.get("commands", list())
        events = domain.get("events", list())
        domain_name = domain["domain"]

        return cls(
            domain_name,
            domain.get("description"),
            domain.get("experimental", False),
            domain.get("dependencies", list()),
            [CdpType.from_json(type, domain_name) for type in types],
            [CdpCommand.from_json(command, domain_name) for command in commands],
            [CdpEvent.from_json(event, domain_name) for event in events],
        )

    def generate_code(self) -> str:
        """Generate the Python module code for a given CDP domain."""
        exp = " (experimental)" if self.experimental else ""
        code = MODULE_HEADER.format(self.domain, exp)
        import_code = self.generate_imports()
        if import_code:
            code += import_code
            code += "\n\n"
        code += "\n"
        item_iter: typing.Iterator[typing.Union[CdpEvent, CdpCommand, CdpType]] = (
            itertools.chain(
                iter(self.types),
                iter(self.commands),
                iter(self.events),
            )
        )
        code += "\n\n\n".join(item.generate_code() for item in item_iter)
        code += "\n"
        return code

    def generate_imports(self) -> str:
        """
        Determine which modules this module depends on and emit the code to
        import those modules.

        Notice that CDP defines a ``dependencies`` field for each domain, but
        these dependencies are a subset of the modules that we actually need to
        import to make our Python code work correctly and type safe. So we
        ignore the CDP's declared dependencies and compute them ourselves.
        """
        refs = set()
        needs_deprecation = False
        for type_ in self.types:
            refs |= type_.get_refs()
        for command in self.commands:
            refs |= command.get_refs()
            if command.deprecated:
                needs_deprecation = True
        for event in self.events:
            refs |= event.get_refs()
            if event.deprecated:
                needs_deprecation = True
        dependencies = set()
        for ref in refs:
            try:
                domain, _ = ref.split(".")
            except ValueError:
                continue
            if domain != self.domain:
                dependencies.add(snake_case(domain))
        code = "\n".join(f"from . import {d}" for d in sorted(dependencies))

        if needs_deprecation:
            code += "\nfrom deprecated.sphinx import deprecated # type: ignore"

        return code


def parse(json_path: Path, output_path: Path) -> typing.List[CdpDomain]:
    """
    Parse JSON protocol description and return domain objects.

    :param Path json_path: path to a JSON CDP schema
    :param Path output_path: a directory path to create the modules in
    :returns: a list of CDP domain objects
    """
    global current_version
    with json_path.open() as json_file:
        schema = json.load(json_file)
    version = schema["version"]
    assert (version["major"], version["minor"]) == ("1", "3")
    current_version = f'{version["major"]}.{version["minor"]}'
    domains = list()
    for domain in schema["domains"]:
        domains.append(CdpDomain.from_json(domain))
    return domains


def generate_init(init_path: Path, domains: typing.List[CdpDomain]) -> None:
    """
    Generate an ``__init__.py`` that exports the specified modules.

    :param Path init_path: a file path to create the init file in
    :param list[tuple] domains: a list of modules each represented as tuples
        of (name, list_of_exported_symbols)
    """
    with init_path.open("w") as init_file:
        init_file.write(INIT_HEADER)
        init_file.write(
            "from . import ({})".format(", ".join(domain.module for domain in domains))
        )


def fix_protocol_spec(domains: typing.List[CdpDomain]) -> None:
    """Fixes following errors in the official CDP spec:
    1. DOM includes an erroneous $ref that refers to itself.
    2. Page includes an event with an extraneous backtick in the description.
    3. Network.Cookie.expires is optional because sometimes its value can be null."""
    for domain in domains:
        if domain.domain == "DOM":
            for cmd in domain.commands:
                if cmd.name == "resolveNode":
                    # Patch 1
                    cmd.parameters[1].ref = "BackendNodeId"
                    break
        elif domain.domain == "Page":
            for event in domain.events:
                if event.name == "screencastVisibilityChanged":
                    # Patch 2
                    event.description = (
                        event.description if event.description else ""
                    ).replace("`", "")
                    break
        elif domain.domain == "Network":
            for _type in domain.types:
                if _type.id == "Cookie":
                    for prop in _type.properties:
                        if prop.name == "expires":
                            prop.optional = True
                            break


def format(output_path: Path) -> None:
    """Format the generated code."""
    subprocess.run(
        [
            "uv",
            "run",
            "ruff",
            "format",
            str(output_path),
            "--config",
            str(REPO_ROOT / "pyproject.toml"),
        ],
        check=True,
    )


def main() -> None:
    """Generate CDP types and docs for ourselves"""
    json_paths = [
        REPO_ROOT / "browser_protocol.json",
        REPO_ROOT / "js_protocol.json",
    ]
    for p in json_paths:
        if not p.exists():
            dl_file(p.name)

    output_path = REPO_ROOT / "zendriver" / "cdp"

    if output_path.exists():
        shutil.rmtree(output_path)
    output_path.mkdir(exist_ok=True)
    try:
        # Parse domains
        domains = list()
        for json_path in json_paths:
            logger.info("Parsing JSON file %s", json_path)
            domains.extend(parse(json_path, output_path))
        domains.sort(key=operator.attrgetter("domain"))
        fix_protocol_spec(domains)
        for domain in domains:
            logger.info("Generating module: %s → %s.py", domain.domain, domain.module)
            module_path = output_path / f"{domain.module}.py"
            with module_path.open("w") as module_file:
                module_file.write(domain.generate_code())

        generate_init(output_path / "__init__.py", domains)
        (output_path / "README.md").write_text(GENERATED_PACKAGE_NOTICE)
        (output_path / "py.typed").touch()

        from textwrap import dedent

        util_path = output_path / "util.py"
        util_path.unlink(missing_ok=True)
        util_path.touch(exist_ok=True)
        util_path.write_text(
            dedent(
                """
            import typing

            T_JSON_DICT = typing.Dict[str, typing.Any]
            _event_parsers = dict()


            def event_class(method):
                ''' A decorator that registers a class as an event class. '''
                def decorate(cls):
                    _event_parsers[method] = cls
                    return cls
                return decorate


            def parse_json_event(json: T_JSON_DICT) -> typing.Any:
                ''' Parse a JSON dictionary into a CDP event. '''
                return _event_parsers[json['method']].from_json(json['params'])
            """
            )
        )
        format(output_path)

    finally:
        list(map(lambda x: x.unlink(), json_paths))


def dl_file(filename: str, path: Path | None = None) -> None:
    urllib.request.urlretrieve(
        f"https://raw.githubusercontent.com/ChromeDevTools/devtools-protocol/master/json/{filename}",
        filename=path or filename,
    )


if __name__ == "__main__":
    main()
