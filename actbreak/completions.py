from __future__ import annotations

import argparse


def _extract(parser: argparse.ArgumentParser) -> tuple[list[str], list[str], dict[str, list[str]]]:
    top_flags: list[str] = []
    commands: list[str] = []
    cmd_flags: dict[str, list[str]] = {}

    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for cmd, subparser in action.choices.items():
                commands.append(cmd)
                flags: list[str] = []
                for subaction in subparser._actions:
                    if subaction.option_strings and subaction.option_strings[0] != "-h":
                        flags.extend(subaction.option_strings)
                cmd_flags[cmd] = flags
        elif action.option_strings and action.option_strings[0] != "-h":
            top_flags.extend(action.option_strings)

    return top_flags, commands, cmd_flags


def generate_bash(parser: argparse.ArgumentParser) -> str:
    top_flags, commands, cmd_flags = _extract(parser)

    cases = []
    for cmd, flags in cmd_flags.items():
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
    top_flags_str = " ".join(top_flags)

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


def generate_zsh(parser: argparse.ArgumentParser) -> str:
    top_flags, commands, cmd_flags = _extract(parser)

    cases = []
    for cmd, flags in cmd_flags.items():
        if flags:
            flag_args = " ".join(f'"{f}"' for f in flags)
            cases.append(
                f'''        {cmd})
            _arguments {flag_args}
            ;;'''
            )
        else:
            cases.append(
                f'''        {cmd})
            ;;'''
            )

    cases_str = "\n".join(cases)
    top_flag_args = " ".join(f'"{f}"' for f in top_flags)
    commands_args = " ".join(f'"{c}"' for c in commands)

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

_actbreak "$@"
'''
