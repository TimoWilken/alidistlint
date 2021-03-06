#!/usr/bin/env python3

'''Lint alidist recipes using yamllint and shellcheck.'''

import os.path
from typing import BinaryIO, Callable, Iterable, NamedTuple

import yaml

GCC_LEVELS: dict[str, str] = {
    'error': 'error',
    'warning': 'warning',
    'info': 'note',
    'style': 'note',
}

GITHUB_LEVELS: dict[str, str] = {
    'error': 'error',
    'warning': 'warning',
    'info': 'notice',
    'style': 'notice',
}

FileParts = dict[str, tuple[str, int, int, bytes | None]]
'''Map temporary file name to original file name and line/column offsets.

For FileParts of YAML header data, also includes the content of the file part,
for direct processing. For FileParts of scripts, this is None instead.
'''


class Error(NamedTuple):
    '''A linter message.

    Instances should contain line and column numbers relative to the original
    input file, not relative to any FileParts that might have been used.
    '''
    level: str
    message: str
    file_name: str
    line: int
    column: int
    end_line: int | None = None
    end_column: int | None = None

    def format_gcc(self) -> str:
        '''Turn the Error into a string like a GCC error message.'''
        return (f'{self.file_name}:{self.line}:{self.column}: '
                f'{GCC_LEVELS[self.level]}: {self.message}')

    def format_github(self) -> str:
        '''Turn the Error into a string that GitHub understands.

        If printed from a GitHub Action, this will show the error messages in
        the Files view.
        '''
        end_line = '' if self.end_line is None else f',endLine={self.end_line}'
        end_column = '' if self.end_column is None else \
            f',endColumn={self.end_column}'
        return (f'::{GITHUB_LEVELS[self.level]} file={self.file_name}'
                f',line={self.line}{end_line}'
                f',col={self.column}{end_column}::{self.message}')


ERROR_FORMATTERS: dict[str, Callable[[Error], str]] = {
    'gcc': Error.format_gcc,
    'github': Error.format_github,
}


# pylint: disable=too-many-ancestors
class TrackedLocationLoader(yaml.loader.SafeLoader):
    '''Load YAML documents while keeping track of keys' line and column.

    We need to override construct_sequence to track the location of list items,
    and construct_mapping to track the location of keys.

    See also: https://stackoverflow.com/q/13319067
    '''
    def construct_sequence(self, node, deep=False):
        sequence = super().construct_sequence(node, deep)
        sequence.append([item_node.start_mark for item_node in node.value])
        return sequence

    def construct_mapping(self, node, deep=False):
        mapping = super().construct_mapping(node, deep=deep)
        mapping['_locations'] = {
            # Keys aren't necessarily strings, so parse them in YAML.
            self.construct_object(key_node): key_node.start_mark
            for key_node, _ in node.value
        }
        return mapping

    @staticmethod
    def remove_trackers(data):
        '''Remove temporary location tracker items.

        Original file locations are tracked using special properties and list
        items and used for more informative error messages, but they should not
        be present for schema validation, for example.
        '''
        if isinstance(data, dict):
            return {key: TrackedLocationLoader.remove_trackers(value)
                    for key, value in data.items()
                    if key != '_locations'}
        if isinstance(data, list):
            return [TrackedLocationLoader.remove_trackers(value)
                    for value in data[:-1]]
        return data


def split_files(temp_dir: str, input_files: Iterable[BinaryIO]) \
        -> tuple[FileParts, FileParts]:
    '''Split every given file into its YAML header and script part.'''
    header_parts: FileParts = {}
    script_parts: FileParts = {}
    for input_file in input_files:
        orig_basename = os.path.basename(input_file.name)
        recipe = input_file.read()
        # Get the first byte of the '---\n' line (excluding the prev newline).
        separator_position = recipe.find(b'\n---\n') + 1
        yaml_text = recipe[:separator_position]

        # Extract the complete YAML header and store its text for later parsing.
        with open(f'{temp_dir}/{orig_basename}.head.yaml', 'wb') as headerf:
            headerf.write(yaml_text)
            header_parts[headerf.name] = input_file.name, 0, 0, yaml_text

        # Extract the main recipe script.
        with open(f'{temp_dir}/{orig_basename}.script.sh', 'wb') as scriptf:
            scriptf.write(recipe[separator_position + 4:])
            # Add 1 to line offset for the separator line.
            script_parts[scriptf.name] = \
                input_file.name, yaml_text.count(b'\n') + 1, 0, None

        # Extract recipes embedded in YAML header, e.g. incremental_recipe.
        try:
            tagged_data = yaml.load(yaml_text, TrackedLocationLoader)
        except yaml.YAMLError:
            # If we can't even load the YAML, skip checking embedded recipes.
            continue
        if not isinstance(tagged_data, dict):
            # Something went wrong loading the YAML -- maybe the '---' line
            # isn't present.
            continue
        for recipe_key, recipe in tagged_data.items():
            if not isinstance(recipe_key, str):
                continue
            if not (recipe_key.endswith('_recipe') or
                    recipe_key.endswith('_check')):
                continue
            line_offset, column_offset = position_of_key(tagged_data,
                                                         (recipe_key,))
            line_offset += 1     # assume values start on a new line
            line_offset -= 1     # first line is 1, but this is an offset
            column_offset += 2   # yamllint requires 2-space indents
            column_offset -= 1   # first column is 1, but this is an offset
            with open(f'{temp_dir}/{orig_basename}.{recipe_key}.sh', 'w',
                      encoding='utf-8') as scriptf:
                scriptf.write(recipe)
                script_parts[scriptf.name] = \
                    input_file.name, line_offset, column_offset, None

    return header_parts, script_parts


def position_of_key(tagged_object: dict,
                    path: tuple[str | int, ...]) -> tuple[int, int]:
    '''Find the line and column numbers of the specified key.'''
    cur_object_parent = tagged_object
    for path_element in path[:-1]:
        cur_object_parent = cur_object_parent[path_element]
    if isinstance(cur_object_parent, dict):
        direct_parent = cur_object_parent['_locations']
    elif isinstance(cur_object_parent, list):
        direct_parent = cur_object_parent[-1]
    else:
        raise TypeError(cur_object_parent)
    try:
        mark = direct_parent[path[-1]]
        return mark.line + 1, mark.column + 1
    except KeyError:
        # The key is not present, but probably required.
        return 1, 0
