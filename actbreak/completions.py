"""Shell completion scripts generated from the argparse parser itself, so a
new flag or subcommand shows up in completions without anyone remembering to
edit a second copy of the CLI."""

from __future__ import annotations

import argparse


def _extract(
    parser: argparse.ArgumentParser,
) -> tuple[list[argparse.Action], list[str], dict[str, argparse.ArgumentParser]]:
    """Return (top-level flag actions, subcommand names, subparser by name)."""
    top_flags: list[argparse.Action] = []
    commands: list[str] = []
    subparsers: dict[str, argparse.ArgumentParser] = {}

    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for cmd, subparser in action.choices.items():
                commands.append(cmd)
                subparsers[cmd] = subparser
        elif action.option_strings and action.option_strings[0] != "-h":
            top_flags.append(action)

    return top_flags, commands, subparsers


def _flag_actions(parser: argparse.ArgumentParser) -> list[argparse.Action]:
    return [a for a in parser._actions if a.option_strings and a.option_strings[0] != "-h"]


def _positional_actions(parser: argparse.ArgumentParser) -> list[argparse.Action]:
    return [a for a in parser._actions if not a.option_strings]


def _option_strings(actions: list[argparse.Action]) -> list[str]:
    out: list[str] = []
    for action in actions:
        out.extend(action.option_strings)
    return out


def generate_bash(parser: argparse.ArgumentParser) -> str:
    top_flag_actions, commands, subparsers = _extract(parser)

    cases = []
    for cmd in commands:
        flags = _option_strings(_flag_actions(subparsers[cmd]))
        if flags:
            flags_str = " ".join(flags)
            cases.append(
                f'''        {cmd})
            COMPREPLY=( $(compgen -W "{flags_str}" -- "$cur") )
            return 0
            ;;'''
            )
        else:
            cases.append(
                f'''        {cmd})
            return 0
            ;;'''
            )

    cases_str = "\n".join(cases)
    commands_str = " ".join(commands)
    top_flags_str = " ".join(_option_strings(top_flag_actions))

    return f'''\
_actbreak() {{
    local cur prev words cword
    COMPREPLY=()
    cur="${{COMP_WORDS[COMP_CWORD]}}"

    local commands="{commands_str}"
    local top_flags="{top_flags_str}"

    if [[ ${{COMP_CWORD}} -eq 1 ]]; then
        if [[ "$cur" == -* ]]; then
            COMPREPLY=( $(compgen -W "$top_flags" -- "$cur") )
        else
            COMPREPLY=( $(compgen -W "$commands" -- "$cur") )
        fi
        return 0
    fi

    local command="${{COMP_WORDS[1]}}"
    if [[ "$cur" == -* ]]; then
        case "$command" in
{cases_str}
        esac
    fi
    return 0
}}
complete -F _actbreak actbreak
'''


def _zsh_description(action: argparse.Action) -> str:
    """argparse help text, made safe to sit inside an _arguments `[...]`
    description: zsh ends the description at the first unescaped `]`, and the
    whole spec is wrapped in single quotes."""
    text = (action.help or "").replace("%(default)s", str(action.default))
    text = " ".join(text.split())
    return text.replace("[", "(").replace("]", ")").replace("'", "")


def _zsh_flag_specs(action: argparse.Action) -> list[str]:
    """One `_arguments` spec per option string. A flag that takes a value gets
    a `:metavar:` tail so zsh knows to expect one instead of offering the next
    flag; without it zsh treats every flag as a bare switch."""
    description = _zsh_description(action)
    # argparse sets nargs=0 for store_true/store_const; everything else here
    # consumes a value.
    takes_value = action.nargs != 0
    repeatable = isinstance(action, argparse._AppendAction)

    value = ""
    if takes_value:
        metavar = action.metavar or action.dest.upper()
        if action.choices:
            value = f":{metavar}:({' '.join(str(c) for c in action.choices)})"
        else:
            value = f":{metavar}:"

    prefix = "*" if repeatable else ""
    return [f"'{prefix}{opt}[{description}]{value}'" for opt in action.option_strings]


def _zsh_positional_specs(parser: argparse.ArgumentParser) -> list[str]:
    """Complete a subcommand's positional arguments as filenames. Both
    commands that take one (`run`, `steps`) take a workflow path."""
    specs = []
    for action in _positional_actions(parser):
        specs.append(f"':{action.dest}:_files'")
    return specs


def _zsh_specs(parser: argparse.ArgumentParser) -> list[str]:
    specs = _zsh_positional_specs(parser)
    for action in _flag_actions(parser):
        specs.extend(_zsh_flag_specs(action))
    return specs


def generate_zsh(parser: argparse.ArgumentParser) -> str:
    top_flag_actions, commands, subparsers = _extract(parser)

    cases = []
    for cmd in commands:
        specs = _zsh_specs(subparsers[cmd])
        if specs:
            joined = " \\\n                        ".join(specs)
            cases.append(
                f'''                {cmd})
                    _arguments \\
                        {joined}
                    ;;'''
            )
        else:
            cases.append(
                f'''                {cmd})
                    ;;'''
            )

    cases_str = "\n".join(cases)
    top_specs: list[str] = []
    for action in top_flag_actions:
        top_specs.extend(_zsh_flag_specs(action))
    top_flag_args = " \\\n        ".join(top_specs)
    commands_args = " ".join(f'"{c}"' for c in commands)

    # `#compdef actbreak` makes this work when it's dropped in $fpath as
    # `_actbreak`; the trailing `compdef` call makes `source <(...)` work too.
    # Calling `_actbreak "$@"` here instead (the obvious-looking thing) runs
    # _arguments outside a completion context, which errors and registers
    # nothing.
    return f'''\
#compdef actbreak

_actbreak() {{
    local context state state_descr line
    typeset -A opt_args

    _arguments -C \\
        {top_flag_args} \\
        '1: :->cmds' \\
        '*::arg:->args'

    case $state in
        cmds)
            _values "actbreak command" {commands_args}
            ;;
        args)
            case $line[1] in
{cases_str}
            esac
            ;;
    esac
}}

compdef _actbreak actbreak
'''
