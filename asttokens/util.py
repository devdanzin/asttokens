# Copyright 2016 Grist Labs, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import ast
import collections
import itertools
import sys
import token
import tokenize
from abc import ABCMeta
from ast import Module, expr, AST
from typing import Callable, Dict, Iterator, List, Optional, Tuple, Union, cast, Any

import six
from astroid.node_classes import NodeNG  # type: ignore[import]
from six import iteritems


def token_repr(tok_type, string):
  # type: (int, Optional[str]) -> str
  """Returns a human-friendly representation of a token with the given type and string."""
  # repr() prefixes unicode with 'u' on Python2 but not Python3; strip it out for consistency.
  return '%s:%s' % (token.tok_name[tok_type], repr(string).lstrip('u'))


class Token(collections.namedtuple('Token', 'type string start end line index startpos endpos')):
  """
  TokenInfo is an 8-tuple containing the same 5 fields as the tokens produced by the tokenize
  module, and 3 additional ones useful for this module:

  - [0] .type     Token type (see token.py)
  - [1] .string   Token (a string)
  - [2] .start    Starting (row, column) indices of the token (a 2-tuple of ints)
  - [3] .end      Ending (row, column) indices of the token (a 2-tuple of ints)
  - [4] .line     Original line (string)
  - [5] .index    Index of the token in the list of tokens that it belongs to.
  - [6] .startpos Starting character offset into the input text.
  - [7] .endpos   Ending character offset into the input text.
  """
  def __str__(self):
    # type: () -> str
    return token_repr(self.type, self.string)


# Type class used to expand out the definition of AST to include fields added by this library
# It's not actually used for anything other than type checking though!
class EnhancedAST(AST):
  # Additional attributes set by mark_tokens
  first_token = None # type: Token 
  last_token = None # type: Token
  lineno = 0 # type: int


if sys.version_info >= (3, 6):
  AstConstant = ast.Constant
else:
  class AstConstant:
    value = object()


AstNode = Union[EnhancedAST, NodeNG]


def match_token(token, tok_type, tok_str=None):
  # type: (Token, int, Optional[str]) -> bool
  """Returns true if token is of the given type and, if a string is given, has that string."""
  return token.type == tok_type and (tok_str is None or token.string == tok_str)


def expect_token(token, tok_type, tok_str=None):
  # type: (Token, int, Optional[str]) -> None
  """
  Verifies that the given token is of the expected type. If tok_str is given, the token string
  is verified too. If the token doesn't match, raises an informative ValueError.
  """
  if not match_token(token, tok_type, tok_str):
    raise ValueError("Expected token %s, got %s on line %s col %s" % (
      token_repr(tok_type, tok_str), str(token),
      token.start[0], token.start[1] + 1))

# These were previously defined in tokenize.py and distinguishable by being greater than
# token.N_TOKEN. As of python3.7, they are in token.py, and we check for them explicitly.
if sys.version_info >= (3, 7):
  def is_non_coding_token(token_type):
    # type: (int) -> bool
    """
    These are considered non-coding tokens, as they don't affect the syntax tree.
    """
    return token_type in (token.NL, token.COMMENT, token.ENCODING)
else:
  def is_non_coding_token(token_type):
    # type: (int) -> bool
    """
    These are considered non-coding tokens, as they don't affect the syntax tree.
    """
    return token_type >= token.N_TOKENS


def iter_children_func(node):
  # type: (Module) -> Callable
  """
  Returns a function which yields all direct children of a AST node,
  skipping children that are singleton nodes.
  The function depends on whether ``node`` is from ``ast`` or from the ``astroid`` module.
  """
  return iter_children_astroid if hasattr(node, 'get_children') else iter_children_ast


def iter_children_astroid(node):
  # type: (NodeNG) -> Union[Iterator, List]
  # Don't attempt to process children of JoinedStr nodes, which we can't fully handle yet.
  if is_joined_str(node):
    return []

  return node.get_children()


SINGLETONS = {c for n, c in iteritems(ast.__dict__) if isinstance(c, type) and
              issubclass(c, (ast.expr_context, ast.boolop, ast.operator, ast.unaryop, ast.cmpop))}

def iter_children_ast(node):
  # type: (AST) -> Iterator[Union[AST, expr]]
  # Don't attempt to process children of JoinedStr nodes, which we can't fully handle yet.
  if is_joined_str(node):
    return

  if isinstance(node, ast.Dict):
    # override the iteration order: instead of <all keys>, <all values>,
    # yield keys and values in source order (key1, value1, key2, value2, ...)
    for (key, value) in zip(node.keys, node.values):
      if key is not None:
        yield key
      yield value
    return

  for child in ast.iter_child_nodes(node):
    # Skip singleton children; they don't reflect particular positions in the code and break the
    # assumptions about the tree consisting of distinct nodes. Note that collecting classes
    # beforehand and checking them in a set is faster than using isinstance each time.
    if child.__class__ not in SINGLETONS:
      yield child


stmt_class_names = {n for n, c in iteritems(ast.__dict__)
                    if isinstance(c, type) and issubclass(c, ast.stmt)}
expr_class_names = ({n for n, c in iteritems(ast.__dict__)
                    if isinstance(c, type) and issubclass(c, ast.expr)} |
                    {'AssignName', 'DelName', 'Const', 'AssignAttr', 'DelAttr'})

# These feel hacky compared to isinstance() but allow us to work with both ast and astroid nodes
# in the same way, and without even importing astroid.
def is_expr(node):
  # type: (AstNode) -> bool
  """Returns whether node is an expression node."""
  return node.__class__.__name__ in expr_class_names

def is_stmt(node):
  # type: (AstNode) -> bool
  """Returns whether node is a statement node."""
  return node.__class__.__name__ in stmt_class_names

def is_module(node):
  # type: (AstNode) -> bool
  """Returns whether node is a module node."""
  return node.__class__.__name__ == 'Module'

def is_joined_str(node):
  # type: (AstNode) -> bool
  """Returns whether node is a JoinedStr node, used to represent f-strings."""
  # At the moment, nodes below JoinedStr have wrong line/col info, and trying to process them only
  # leads to errors.
  return node.__class__.__name__ == 'JoinedStr'


def is_starred(node):
  # type: (AstNode) -> bool
  """Returns whether node is a starred expression node."""
  return node.__class__.__name__ == 'Starred'


def is_slice(node):
  # type: (AstNode) -> bool
  """Returns whether node represents a slice, e.g. `1:2` in `x[1:2]`"""
  # Before 3.9, a tuple containing a slice is an ExtSlice,
  # but this was removed in https://bugs.python.org/issue34822
  return (
      node.__class__.__name__ in ('Slice', 'ExtSlice')
      or (
          node.__class__.__name__ == 'Tuple'
          and any(map(is_slice, cast(ast.Tuple, node).elts))
      )
  )


# Sentinel value used by visit_tree().
_PREVISIT = object()

def visit_tree(node, previsit, postvisit):
  # type: (Module, Callable[[AstNode, Optional[Token]], Tuple[Optional[Token], Optional[Token]]], Optional[Callable[[AstNode, Optional[Token], Optional[Token]], None]])   -> None
  """
  Scans the tree under the node depth-first using an explicit stack. It avoids implicit recursion
  via the function call stack to avoid hitting 'maximum recursion depth exceeded' error.

  It calls ``previsit()`` and ``postvisit()`` as follows:

  * ``previsit(node, par_value)`` - should return ``(par_value, value)``
        ``par_value`` is as returned from ``previsit()`` of the parent.

  * ``postvisit(node, par_value, value)`` - should return ``value``
        ``par_value`` is as returned from ``previsit()`` of the parent, and ``value`` is as
        returned from ``previsit()`` of this node itself. The return ``value`` is ignored except
        the one for the root node, which is returned from the overall ``visit_tree()`` call.

  For the initial node, ``par_value`` is None. ``postvisit`` may be None.
  """
  if not postvisit:
    postvisit = lambda node, pvalue, value: None

  iter_children = iter_children_func(node)
  done = set()
  ret = None
  stack = [(node, None, _PREVISIT)] # type: List[Tuple[AstNode, Optional[Token], Union[Optional[Token], object]]]
  while stack:
    current, par_value, value = stack.pop()
    if value is _PREVISIT:
      assert current not in done    # protect againt infinite loop in case of a bad tree.
      done.add(current)

      pvalue, post_value = previsit(current, par_value)
      stack.append((current, par_value, post_value))

      # Insert all children in reverse order (so that first child ends up on top of the stack).
      ins = len(stack)
      for n in iter_children(current):
        stack.insert(ins, (n, pvalue, _PREVISIT))
    else:
      ret = postvisit(current, par_value, cast(Optional[Token], value))
  return ret



def walk(node):
  # type: (Module) -> Iterator[Union[Module, AstNode]]
  """
  Recursively yield all descendant nodes in the tree starting at ``node`` (including ``node``
  itself), using depth-first pre-order traversal (yieling parents before their children).

  This is similar to ``ast.walk()``, but with a different order, and it works for both ``ast`` and
  ``astroid`` trees. Also, as ``iter_children()``, it skips singleton nodes generated by ``ast``.
  """
  iter_children = iter_children_func(node)
  done = set()
  stack = [node]
  while stack:
    current = stack.pop()
    assert current not in done    # protect againt infinite loop in case of a bad tree.
    done.add(current)

    yield current

    # Insert all children in reverse order (so that first child ends up on top of the stack).
    # This is faster than building a list and reversing it.
    ins = len(stack)
    for c in iter_children(current):
      stack.insert(ins, c)


def replace(text, replacements):
  # type: (str, List[Tuple[int, int, str]]) -> str
  """
  Replaces multiple slices of text with new values. This is a convenience method for making code
  modifications of ranges e.g. as identified by ``ASTTokens.get_text_range(node)``. Replacements is
  an iterable of ``(start, end, new_text)`` tuples.

  For example, ``replace("this is a test", [(0, 4, "X"), (8, 9, "THE")])`` produces
  ``"X is THE test"``.
  """
  p = 0
  parts = []
  for (start, end, new_text) in sorted(replacements):
    parts.append(text[p:start])
    parts.append(new_text)
    p = end
  parts.append(text[p:])
  return ''.join(parts)


class NodeMethods(object):
  """
  Helper to get `visit_{node_type}` methods given a node's class and cache the results.
  """
  def __init__(self):
    # type: () -> None
    self._cache = {} # type: Dict[Union[ABCMeta, type], Callable[[AstNode, Token, Token], Tuple[Token, Token]]]

  def get(self, obj, cls):
    # type: (Any, Union[ABCMeta, type]) -> Callable
    """
    Using the lowercase name of the class as node_type, returns `obj.visit_{node_type}`,
    or `obj.visit_default` if the type-specific method is not found.
    """
    method = self._cache.get(cls)
    if not method:
      name = "visit_" + cls.__name__.lower()
      method = getattr(obj, name, obj.visit_default)
      self._cache[cls] = method
    return method


def patched_generate_tokens(original_tokens):
  # type: (Iterator[tokenize.TokenInfo]) -> Iterator[tokenize.TokenInfo]
  """
  Fixes tokens yielded by `tokenize.generate_tokens` to handle more non-ASCII characters in identifiers.
  Workaround for https://github.com/python/cpython/issues/68382.
  Should only be used when tokenizing a string that is known to be valid syntax,
  because it assumes that error tokens are not actually errors.
  Combines groups of consecutive NAME and/or ERRORTOKEN tokens into a single NAME token.
  """
  if six.PY2:
    # Python 2 doesn't support non-ASCII identifiers, and making the rest of this code support Python 2
    # means working with raw tuples instead of tokenize.TokenInfo namedtuples.
    for tok in original_tokens:
      yield tok
    return

  for key, group_iter in itertools.groupby(
      original_tokens,
      lambda t: tokenize.NAME if t.type == tokenize.ERRORTOKEN else t.type,
  ):
    group = list(group_iter)  # type: List[tokenize.TokenInfo]
    if key == tokenize.NAME and len(group) > 1 and any(tok.type == tokenize.ERRORTOKEN for tok in group):
      line = group[0].line  # type: str
      assert {tok.line for tok in group} == {line}
      yield tokenize.TokenInfo(
        type=tokenize.NAME,
        string="".join(t.string for t in group),
        start=group[0].start,
        end=group[-1].end,
        line=line,
      )
    else:
      for tok in group:
        yield tok
