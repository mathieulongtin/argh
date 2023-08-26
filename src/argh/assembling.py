#
#  Copyright © 2010—2023 Andrey Mikhaylenko and contributors
#
#  This file is part of Argh.
#
#  Argh is free software under terms of the GNU Lesser
#  General Public License version 3 (LGPLv3) as published by the Free
#  Software Foundation. See the file README.rst for copying conditions.
#
"""
Assembling
~~~~~~~~~~

Functions and classes to properly assemble your commands in a parser.
"""
import warnings
from collections import OrderedDict

from argh.completion import COMPLETION_ENABLED
from argh.constants import (
    ATTR_ALIASES,
    ATTR_ARGS,
    ATTR_EXPECTS_NAMESPACE_OBJECT,
    ATTR_NAME,
    DEFAULT_ARGUMENT_TEMPLATE,
    DEST_FUNCTION,
    PARSER_FORMATTER,
)
from argh.exceptions import AssemblingError
from argh.utils import get_arg_spec, get_subparsers

__all__ = [
    "SUPPORTS_ALIASES",
    "set_default_command",
    "add_commands",
    "add_subcommands",
]


# TODO: remove in v.0.30.
SUPPORTS_ALIASES = True
"""
.. deprecated:: 0.28.0

    This constant will be removed in Argh v.0.30.

    It's not relevant anymore because it's always `True` for all Python
    versions currently supported by Argh.

"""


def _get_args_from_signature(function):
    if getattr(function, ATTR_EXPECTS_NAMESPACE_OBJECT, False):
        return

    spec = get_arg_spec(function)

    defaults = dict(zip(*[reversed(x) for x in (spec.args, spec.defaults or [])]))
    defaults.update(getattr(spec, "kwonlydefaults", None) or {})

    kwonly = getattr(spec, "kwonlyargs", [])

    annotations = dict(
        (k, v) for k, v in function.__annotations__.items() if isinstance(v, str)
    )

    # define the list of conflicting option strings
    # (short forms, i.e. single-character ones)
    chars = [a[0] for a in spec.args + kwonly]
    char_counts = dict((char, chars.count(char)) for char in set(chars))
    conflicting_opts = tuple(char for char in char_counts if 1 < char_counts[char])

    for name in spec.args + kwonly:
        flags = []  # name_or_flags
        akwargs = {}  # keyword arguments for add_argument()

        # TODO: remove this in v.0.30.
        if name in annotations:
            # help message:  func(a : "b")  ->  add_argument("a", help="b")
            value = annotations.get(name)
            if isinstance(value, str):
                warnings.warn(
                    "Defining argument help messages via annotations is "
                    + "deprecated and will be removed in Argh 0.30.  Please "
                    + 'replace `f(a:"foo")` with `@arg("-a", help="foo")(a)`.',
                    DeprecationWarning,
                )
                akwargs.update(help=value)

        if name in defaults or name in kwonly:
            if name in defaults:
                akwargs.update(default=defaults.get(name))
            else:
                akwargs.update(required=True)
            flags = ("-{0}".format(name[0]), "--{0}".format(name))
            if name.startswith(conflicting_opts):
                # remove short name
                flags = flags[1:]

        else:
            # positional argument
            flags = (name,)

        # cmd(foo_bar)  ->  add_argument('foo-bar')
        flags = tuple(x.replace("_", "-") if x.startswith("-") else x for x in flags)

        yield dict(option_strings=flags, **akwargs)

    if spec.varargs:
        # *args
        yield dict(option_strings=[spec.varargs], nargs="*")


def _guess(kwargs):
    """
    Adds types, actions, etc. to given argument specification.
    For example, ``default=3`` implies ``type=int``.

    :param arg: a :class:`argh.utils.Arg` instance
    """
    guessed = {}

    # Parser actions that accept argument 'type'
    TYPE_AWARE_ACTIONS = "store", "append"

    # guess type/action from default value
    value = kwargs.get("default")
    if value is not None:
        if isinstance(value, bool):
            if kwargs.get("action") is None:
                # infer action from default value
                guessed["action"] = "store_false" if value else "store_true"
        elif kwargs.get("type") is None:
            # infer type from default value
            # (make sure that action handler supports this keyword)
            if kwargs.get("action", "store") in TYPE_AWARE_ACTIONS:
                guessed["type"] = type(value)

    # guess type from choices (first item)
    if kwargs.get("choices") and "type" not in list(guessed) + list(kwargs):
        guessed["type"] = type(kwargs["choices"][0])

    return dict(kwargs, **guessed)


def _is_positional(args, prefix_chars="-"):
    if not args or not args[0]:
        raise ValueError("Expected at least one argument")
    if args[0][0].startswith(tuple(prefix_chars)):
        return False
    else:
        return True


def _get_parser_param_kwargs(parser, argspec):
    argspec = argspec.copy()  # parser methods modify source data
    args = argspec["option_strings"]

    if _is_positional(args, prefix_chars=parser.prefix_chars):
        get_kwargs = parser._get_positional_kwargs
    else:
        get_kwargs = parser._get_optional_kwargs

    kwargs = get_kwargs(*args, **argspec)

    kwargs["dest"] = kwargs["dest"].replace("-", "_")

    return kwargs


def _get_dest(parser, argspec):
    kwargs = _get_parser_param_kwargs(parser, argspec)
    return kwargs["dest"]


def set_default_command(parser, function):
    """
    Sets default command (i.e. a function) for given parser.

    If `parser.description` is empty and the function has a docstring,
    it is used as the description.

    .. note::

       If there are both explicitly declared arguments (e.g. via
       :func:`~argh.decorators.arg`) and ones inferred from the function
       signature, declared ones will be merged into inferred ones.
       If an argument does not conform to the function signature,
       `AssemblingError` is raised.

    .. note::

       If the parser was created with ``add_help=True`` (which is by default),
       option name ``-h`` is silently removed from any argument.

    """
    spec = get_arg_spec(function)

    declared_args = getattr(function, ATTR_ARGS, [])
    inferred_args = list(_get_args_from_signature(function))

    if inferred_args and declared_args:
        # We've got a mixture of declared and inferred arguments

        # a mapping of "dest" strings to argument declarations.
        #
        # * a "dest" string is a normalized form of argument name, i.e.:
        #
        #     '-f', '--foo' → 'foo'
        #     'foo-bar'     → 'foo_bar'
        #
        # * argument declaration is a dictionary representing an argument;
        #   it is obtained either from _get_args_from_signature() or from
        #   an @arg decorator (as is).
        #
        dests = OrderedDict()

        for argspec in inferred_args:
            dest = _get_parser_param_kwargs(parser, argspec)["dest"]
            dests[dest] = argspec

        for declared_kw in declared_args:
            # an argument is declared via decorator
            dest = _get_dest(parser, declared_kw)
            if dest in dests:
                # the argument is already known from function signature
                #
                # now make sure that this declared arg conforms to the function
                # signature and therefore only refines an inferred arg:
                #
                #      @arg('my-foo')    maps to  func(my_foo)
                #      @arg('--my-bar')  maps to  func(my_bar=...)

                # either both arguments are positional or both are optional
                decl_positional = _is_positional(declared_kw["option_strings"])
                infr_positional = _is_positional(dests[dest]["option_strings"])
                if decl_positional != infr_positional:
                    kinds = {True: "positional", False: "optional"}
                    raise AssemblingError(
                        '{func}: argument "{dest}" declared as {kind_i} '
                        "(in function signature) and {kind_d} (via decorator)".format(
                            func=function.__name__,
                            dest=dest,
                            kind_i=kinds[infr_positional],
                            kind_d=kinds[decl_positional],
                        )
                    )

                # merge explicit argument declaration into the inferred one
                # (e.g. `help=...`)
                dests[dest].update(**declared_kw)
            else:
                # the argument is not in function signature
                varkw = getattr(spec, "varkw", getattr(spec, "keywords", []))
                if varkw:
                    # function accepts **kwargs; the argument goes into it
                    dests[dest] = declared_kw
                else:
                    # there's no way we can map the argument declaration
                    # to function signature
                    xs = (dests[x]["option_strings"] for x in dests)
                    raise AssemblingError(
                        "{func}: argument {flags} does not fit "
                        "function signature: {sig}".format(
                            flags=", ".join(declared_kw["option_strings"]),
                            func=function.__name__,
                            sig=", ".join("/".join(x) for x in xs),
                        )
                    )

        # pack the modified data back into a list
        inferred_args = dests.values()

    command_args = inferred_args or declared_args

    # add types, actions, etc. (e.g. default=3 implies type=int)
    command_args = [_guess(x) for x in command_args]

    for draft in command_args:
        draft = draft.copy()
        if "help" not in draft:
            draft.update(help=DEFAULT_ARGUMENT_TEMPLATE)
        dest_or_opt_strings = draft.pop("option_strings")
        if parser.add_help and "-h" in dest_or_opt_strings:
            dest_or_opt_strings = [x for x in dest_or_opt_strings if x != "-h"]
        completer = draft.pop("completer", None)
        try:
            action = parser.add_argument(*dest_or_opt_strings, **draft)
            if COMPLETION_ENABLED and completer:
                action.completer = completer
        except Exception as e:
            raise type(e)(
                "{func}: cannot add arg {args}: {msg}".format(
                    args="/".join(dest_or_opt_strings), func=function.__name__, msg=e
                )
            )

    if function.__doc__ and not parser.description:
        parser.description = function.__doc__
    parser.set_defaults(
        **{
            DEST_FUNCTION: function,
        }
    )


def add_commands(
    parser,
    functions,
    group_name=None,
    group_kwargs=None,
    func_kwargs=None,
    # deprecated args:
    title=None,
    description=None,
    help=None,
    namespace=None,
    namespace_kwargs=None,
):
    """
    Adds given functions as commands to given parser.

    :param parser:

        an :class:`argparse.ArgumentParser` instance.

    :param functions:

        a list of functions. A subparser is created for each of them.
        If the function is decorated with :func:`~argh.decorators.arg`, the
        arguments are passed to :meth:`argparse.ArgumentParser.add_argument`.
        See also :func:`~argh.dispatching.dispatch` for requirements
        concerning function signatures. The command name is inferred from the
        function name. Note that the underscores in the name are replaced with
        hyphens, i.e. function name "foo_bar" becomes command name "foo-bar".

    :param group_name:

        an optional string representing the group of commands. For example, if
        a command named "hello" is added without the group name, it will be
        available as "prog.py hello"; if the group name if specified as "greet",
        then the command will be accessible as "prog.py greet hello". The
        group itself is not callable, so "prog.py greet" will fail and only
        display a help message.

    :param func_kwargs:

        a `dict` of keyword arguments to be passed to each nested ArgumentParser
        instance created per command (i.e. per function).  Members of this
        dictionary have the highest priority, so a function's docstring is
        overridden by a `help` in `func_kwargs` (if present).

    :param group_kwargs:

        a `dict` of keyword arguments to be passed to the nested ArgumentParser
        instance under given `group_name`.

    Deprecated params that should be renamed:

    :param namespace:

        .. deprecated:: 0.29.0

           This argument will be removed in Argh v.0.30.
           Please use `group_name` instead.

    :param namespace_kwargs:

        .. deprecated:: 0.29.0

           This argument will be removed in Argh v.0.30.
           Please use `group_kwargs` instead.

    Deprecated params that should be moved into `group_kwargs`:

    :param title:

        .. deprecated:: 0.26.0

           This argument will be removed in Argh v.0.30.
           Please use `namespace_kwargs` instead.

    :param description:

        .. deprecated:: 0.26.0

           This argument will be removed in Argh v.0.30.
           Please use `namespace_kwargs` instead.

    :param help:

        .. deprecated:: 0.26.0

           This argument will be removed in Argh v.0.30.
           Please use `namespace_kwargs` instead.

    .. note::

        This function modifies the parser object. Generally side effects are
        bad practice but we don't seem to have any choice as ArgumentParser is
        pretty opaque.
        You may prefer :meth:`~argh.helpers.ArghParser.add_commands` for a bit
        more predictable API.

    .. note::

       An attempt to add commands to a parser which already has a default
       function (e.g. added with :func:`~argh.assembling.set_default_command`)
       results in `AssemblingError`.

    """
    group_kwargs = group_kwargs or {}

    # ------------------------------------------------------------------------
    # TODO remove all of these in 0.30
    #
    if namespace:
        warnings.warn(
            "Argument `namespace` is deprecated in add_commands(), "
            + "it will be removed in Argh 0.30. "
            + "Please use `group_name` instead.",
            DeprecationWarning,
        )
        group_name = namespace
    if namespace_kwargs:
        warnings.warn(
            "Argument `namespace_kwargs` is deprecated in add_commands(), "
            + "it will be removed in Argh 0.30. "
            + "Please use `group_kwargs` instead.",
            DeprecationWarning,
        )
        group_kwargs = namespace_kwargs
    if title:
        warnings.warn(
            "Argument `title` is deprecated in add_commands(), "
            + "it will be removed in Argh 0.30. "
            + "Please use `parser_kwargs` instead.",
            DeprecationWarning,
        )
        group_kwargs["description"] = title
    if help:
        warnings.warn(
            "Argument `help` is deprecated in add_commands(), "
            + "it will be removed in Argh 0.30. "
            + "Please use `parser_kwargs` instead.",
            DeprecationWarning,
        )
        group_kwargs["help"] = help
    if description:
        warnings.warn(
            "Argument `description` is deprecated in add_commands(), "
            + "it will be removed in Argh 0.30. "
            + "Please use `parser_kwargs` instead.",
            DeprecationWarning,
        )
        group_kwargs["description"] = description
    #
    # ------------------------------------------------------------------------

    subparsers_action = get_subparsers(parser, create=True)

    if group_name:
        # Make a nested parser and init a deeper _SubParsersAction under it.

        # Create a named group of commands.  It will be listed along with
        # root-level commands in ``app.py --help``; in that context its `title`
        # can be used as a short description on the right side of its name.
        # Normally `title` is shown above the list of commands
        # in ``app.py my-group --help``.
        subsubparser_kw = {
            "help": group_kwargs.get("title"),
        }
        subsubparser = subparsers_action.add_parser(group_name, **subsubparser_kw)
        subparsers_action = subsubparser.add_subparsers(**group_kwargs)
    else:
        if group_kwargs:
            raise ValueError("`group_kwargs` only makes sense with `group_name`.")

    for func in functions:
        cmd_name, func_parser_kwargs = _extract_command_meta_from_func(func)

        # override any computed kwargs by manually supplied ones
        if func_kwargs:
            func_parser_kwargs.update(func_kwargs)

        # create and set up the parser for this command
        command_parser = subparsers_action.add_parser(cmd_name, **func_parser_kwargs)
        set_default_command(command_parser, func)


def _extract_command_meta_from_func(func):
    # use explicitly defined name; if none, use function name (a_b → a-b)
    cmd_name = getattr(func, ATTR_NAME, func.__name__.replace("_", "-"))

    func_parser_kwargs = {
        # add command help from function's docstring
        "help": func.__doc__,
        # set default formatter
        "formatter_class": PARSER_FORMATTER,
    }

    # add aliases for command name
    func_parser_kwargs["aliases"] = getattr(func, ATTR_ALIASES, [])

    return cmd_name, func_parser_kwargs


def add_subcommands(parser, group_name, functions, **group_kwargs):
    """
    A wrapper for :func:`add_commands`.

    These examples are equivalent::

        add_commands(parser, [get, put], group_name="db",
                     group_kwargs={
                         "title": "database commands",
                         "help": "CRUD for our silly database"
                     })

        add_subcommands(parser, "db", [get, put],
                        title="database commands",
                        help="CRUD for our database")

    """
    add_commands(parser, functions, group_name=group_name, group_kwargs=group_kwargs)
