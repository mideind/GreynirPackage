"""

    Greynir: Natural language processing for Icelandic

    High-level wrapper for the Greynir tokenizer, parser and reducer

    Copyright (C) 2021 Miðeind ehf.
    Original author: Vilhjálmur Þorsteinsson

    This software is licensed under the MIT License:

        Permission is hereby granted, free of charge, to any person
        obtaining a copy of this software and associated documentation
        files (the "Software"), to deal in the Software without restriction,
        including without limitation the rights to use, copy, modify, merge,
        publish, distribute, sublicense, and/or sell copies of the Software,
        and to permit persons to whom the Software is furnished to do so,
        subject to the following conditions:

        The above copyright notice and this permission notice shall be
        included in all copies or substantial portions of the Software.

        THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
        EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
        MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
        IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY
        CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
        TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
        SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

    This module implements a high-level interface to the Greynir
    tokenizer, parser and reducer for parsing Icelandic text into
    trees.

"""

from typing import (
    Any,
    Iterable,
    Iterator,
    Optional,
    Union,
    Callable,
    Dict,
    List,
    Tuple,
    NamedTuple,
    Type,
    cast,
)
import time
import operator
import json
from threading import Lock

from tokenizer import Tok, TOK, correct_spaces, paragraphs, mark_paragraphs

from .bintokenizer import (
    tokenize as bin_tokenize,
    TokenList,
    tokens_are_foreign,
    StringIterable,
    load_token,
)
from .fastparser import Fast_Parser, ParseError
from .reducer import Reducer
from .cache import cached_property
from .simpletree import SimpleTree
from .incparser import ICELANDIC_RATIO
from .lemmatize import LemmaTuple, Comparable, simple_lemmatize


# The type of the values generated by the tokenizer.paragraphs() function
SentenceTuple = Tuple[int, TokenList]

GreynirType = Type["Greynir"]

# The Sentence.terminals attribute returns a list of Terminal objects
Terminal = NamedTuple(
    "Terminal",
    [
        ("text", str),
        ("lemma", str),
        ("category", str),
        ("variants", List[str]),
        ("index", int),
    ],
)

# Progress function parameter type
ProgressFunc = Optional[Callable[[float], None]]

# The type of a parse result
ParseResult = Dict[
    str, Union[int, float, Iterable["_Sentence"], Iterable[Iterable["_Sentence"]]]
]

# The default maximum length of a sentence, in tokens, that we attempt to parse
DEFAULT_MAX_SENT_TOKENS = 90


class _Sentence:

    """ A container for a sentence that has been extracted from the
        tokenizer. The sentence can be explicitly parsed by calling
        sentence.parse(). After parsing, a number of query functions
        are available on the parse tree. """

    def __init__(self, job: "_Job", s: TokenList) -> None:
        """ NOTE! If attributes are added here, the _Sentence.load() function
            below needs to be updated accordingly. """
        self._job = job
        # s is a token list
        self._s = s
        self._len = len(s)
        assert self._len > 0  # Input should be already sanitized
        self._err_index: Optional[int] = None
        self._error: Optional[ParseError] = None
        self._tree = self._simplified_tree = None
        # Number of possible combinations
        self._num: Optional[int] = None
        # Score of best parse tree
        self._score: Optional[int] = None
        # Cached terminals
        self._terminals: Optional[List[Terminal]] = None
        if self._job.parse_immediately:
            # We want an immediate parse of the sentence
            self.parse()

    def __len__(self) -> int:
        """ Return the number of tokens in the sentence """
        return self._len

    def parse(self) -> bool:
        """ Parse the sentence """
        if self._num is not None:
            # Already parsed
            return self._num > 0
        job = self._job
        num = 0
        score = 0
        tree = None
        try:
            # Invoke the parser on the sentence tokens
            tree, num, score = job.parse(self._s)
        except ParseError as e:
            self._err_index = self._len - 1 if e.token_index is None else e.token_index
            self._error = e
        self._tree = tree
        if tree is None:
            self._simplified_tree = None
        else:
            # Create a simplified tree as well
            self._simplified_tree = SimpleTree.from_deep_tree(tree, self._s)
        self._num = num
        self._score = score
        return num > 0

    @property
    def error(self) -> Optional[ParseError]:
        """ Return the ParseError that occurred when parsing this sentence, or None """
        return self._error

    @property
    def err_index(self) -> Optional[int]:
        """ Return the index of the error token, if an error occurred;
            otherwise None """
        return self._err_index

    @property
    def tokens(self) -> TokenList:
        """ Return the tokens in the sentence """
        return self._s

    def is_foreign(self, min_icelandic_ratio: float = ICELANDIC_RATIO) -> bool:
        """ Return True if the sentence is probably not in Icelandic """
        return tokens_are_foreign(self.tokens, min_icelandic_ratio)

    @property
    def combinations(self) -> Optional[int]:
        """ Return the number of different parse tree combinations
            for the sentence, or 0 if no parse tree was found,
            or None if the sentence hasn't been parsed """
        return self._num

    @property
    def score(self) -> Optional[int]:
        """ The score of the best parse tree for the sentence """
        return self._score

    @property
    def tree(self) -> Optional[SimpleTree]:
        """ Return the simplified parse tree, or None
            if the sentence hasn't been parsed """
        return self._simplified_tree

    @property
    def deep_tree(self) -> Any:
        """ Return the original deep tree, as constructed by the parser,
            corresponding directly to grammar nonterminals and terminals """
        return self._tree

    @property
    def flat_tree(self) -> Optional[str]:
        """ Return a flat text representation of the simplified parse tree """
        return None if self.tree is None else self.tree.flat

    @cached_property
    def text(self) -> str:
        """ Return a raw text representation of the sentence,
            with spaces between all tokens """
        return " ".join(t.txt for t in self._s if t.txt)

    @property
    def tidy_text(self) -> str:
        """ Return a [more] correctly spaced text representation of the sentence """
        terminals = self.terminals
        if terminals is None:
            # Not parsed (yet)
            txt = self.text
        else:
            # Use the terminal text representation -
            # it's got fancy em/en-dashes and stuff
            txt = " ".join(t.text for t in terminals)
        return correct_spaces(txt)

    @property
    def terminals(self) -> Optional[List[Terminal]]:
        """ Return a list of tuples, one for each terminal in the sentence.
            The tuples contain the original text of the token that matched
            the terminal, the associated word lemma, the category, and a set
            of variants (case, number, gender, etc.) """
        if self.tree is None:
            # Must parse the sentence first, without errors
            return None
        if self._terminals is not None:
            # Already calculated and cached
            return self._terminals
        # Generate the terminal list from the parse tree
        # pylint: disable=not-an-iterable
        self._terminals = [
            Terminal(d.text, d.lemma, d.tcat, d.all_variants, d.index)
            for d in self.terminal_nodes
        ]
        return self._terminals

    @cached_property
    def terminal_nodes(self) -> List[SimpleTree]:
        """ Return a list of the terminal nodes within the parse tree
            for this sentence """
        if self.tree is None:
            return []
        return [d for d in self.tree.descendants if d.is_terminal]

    @property
    def lemmas(self) -> Optional[List[str]]:
        """ Convenience property to return the lemmas only """
        t = self.terminals
        return None if t is None else [terminal.lemma for terminal in t]

    @property
    def categories(self) -> Optional[List[str]]:
        """ Convenience property to return the categories only """
        if self.tree is None:
            return None
        # Note that here we return the BÍN category,
        # not the terminal category (tcat)
        # pylint: disable=not-an-iterable
        return [d.cat for d in self.terminal_nodes]

    @property
    def lemmas_and_cats(self) -> Optional[List[Tuple[str, str]]]:
        """ Convenience property to return (lemma, category) tuples """
        if self.tree is None:
            return None
        # Note that we return the "lemma category", which is suitable for
        # topic indexing and similar applications. Unknown words and entity
        # names have the category 'entity' in this case, and person names
        # have one of 'person_kk'/'person_kvk'/'person_hk'.
        # pylint: disable=not-an-iterable
        return [(d.lemma, d.lemma_cat) for d in self.terminal_nodes]

    @property
    def ifd_tags(self) -> Optional[List[str]]:
        """ Return a list of Icelandic Frequency Dictionary (IFD) tags for
            the terminals/tokens in this sentence. """
        if self.tree is None:
            return None
        # Flatten the ifd_tags lists for the individual nodes
        # (nonterminal nodes return an empty list in the ifd_tags property)
        return [ifd_tag for d in self.tree.descendants for ifd_tag in d.ifd_tags]

    def dump(self, greynir_cls: GreynirType) -> Dict[str, Any]:
        """ Dump internal data of the class instance for serialization.
            Useful for storing parsed data in a database.
            Note: Normally, sentences are dumped using Greynir.dumps_single(). """
        return {
            "tokens": [greynir_cls._dump_token(t) for t in self._s],
            "tree": None if self.tree is None else self.tree._head,
        }

    def dumps(self, greynir_cls: GreynirType, **kwargs) -> str:
        """ Dump internal data of the class instance as a json string.
            Useful for storing parsed data in a database.
            Note: Normally, sentences are dumped using Greynir.dumps_single(). """
        if "ensure_ascii" not in kwargs:
            # Unless explicitly stated, we are OK with UTF-8 in the generated JSON
            return json.dumps(self.dump(greynir_cls), ensure_ascii=False, **kwargs)
        return json.dumps(self.dump(greynir_cls), **kwargs)

    @classmethod
    def load(
        cls, greynir_cls: GreynirType, tokens: List[Tok], tree: Optional[Dict]
    ) -> "_Sentence":
        """ Load previously dumped data.
            Useful for retrieving parsed data from a database.
            Note: Normally, sentences are loaded using Greynir.loads_single(). """
        instance = cls.__new__(cls)
        instance.__dict__ = {
            "_s": [greynir_cls._load_token(*t) for t in tokens],
            "len": len(tokens),
            "_simplified_tree": None if tree is None else SimpleTree([[tree]]),
            "_terminals": None,
            "_job": None,
            "_err_index": None,
            "_error": None,
            "_score": None,
        }
        return instance

    @classmethod
    def loads(cls, greynir_cls: GreynirType, json_str: str, **kwargs) -> "_Sentence":
        """ Load a previously dumped JSON string.
            Useful for retrieving parsed data from a database.
            Note: Normally, sentences are loaded using Greynir.loads_single(). """
        data = json.loads(json_str, **kwargs)
        return cls.load(greynir_cls, **data)

    def __str__(self) -> str:
        """ Return a text representation of a sentence """
        return self.text


# Create a public alias for the _Sentence class
Sentence: Type[_Sentence] = _Sentence


class _NounPhrase(_Sentence):

    """ A specialization for parsed noun phrases,
        providing easy access to inflectional forms """

    _nom = operator.attrgetter("nominative_np")
    _acc = operator.attrgetter("accusative_np")
    _dat = operator.attrgetter("dative_np")
    _gen = operator.attrgetter("genitive_np")
    _ind = operator.attrgetter("indefinite_np")
    _can = operator.attrgetter("canonical_np")

    def _get(self, getter: Any) -> Optional[str]:
        if self.tree is None:
            return None
        return correct_spaces(getter(self.tree))

    @cached_property
    def nominative(self) -> Optional[str]:
        """ Return nominative form (nefnifall) """
        return self._get(self._nom)

    @cached_property
    def indefinite(self) -> Optional[str]:
        """ Return indefinite form (nefnifall án greinis) """
        return self._get(self._ind)

    @cached_property
    def canonical(self) -> Optional[str]:
        """ Return canonical form (nefnifall eintölu án greinis) """
        return self._get(self._can)

    @cached_property
    def accusative(self) -> Optional[str]:
        """ Return accusative form (þolfall) """
        return self._get(self._acc)

    @cached_property
    def dative(self) -> Optional[str]:
        """ Return dative form (þágufall) """
        return self._get(self._dat)

    @cached_property
    def genitive(self) -> Optional[str]:
        """ Return genitive form (eignarfall) """
        return self._get(self._gen)


class _Paragraph:

    """ Encapsulates a paragraph that contains sentences """

    def __init__(self, job: "_Job", p: Iterable[SentenceTuple]) -> None:
        self._job = job
        self._p = p

    def sentences(self) -> Iterable[_Sentence]:
        """ Yield the sentences within the paragraph, nicely wrapped """
        # self._p is a generator that yields (ix, toklist) tuples,
        # where ix is a starting index of the sentence within the
        # token stream, and toklist is the list of tokens in the sentence,
        # not including S_BEGIN and S_END tokens.
        for _, sent in self._p:
            yield self._job._create_sentence(sent)

    def __iter__(self) -> Iterator[_Sentence]:
        """ Allow easy iteration of sentences within this paragraph """
        return iter(self.sentences())


class _Job:

    """ A parsing job object, allowing incremental parsing of text
        by paragraph and/or sentence.
    """

    def __init__(
        self,
        greynir: "Greynir",
        tokens: Iterable[Tok],
        *,
        parse: bool = False,
        root: Optional[str] = None,
        progress_func: ProgressFunc = None,
        max_sent_tokens: int = DEFAULT_MAX_SENT_TOKENS,
    ) -> None:
        self._r = greynir
        self._parser = self._r.parser
        self._reducer = self._r.reducer
        self._tokens = tokens
        self._parse_time = 0.0
        self._reduce_time = 0.0
        self._parse = parse
        # Pre-counted number of sentences (used for progress monitoring)
        # This count includes one initial step, indicating completion of
        # tokenization, so it is the number of sentences + 1
        self._cnt_sent = 0
        # Accumulated number of sentences generated so far
        self._num_sent = 0
        self._num_parsed = 0
        self._num_tokens = 0
        self._num_combinations = 0
        self._total_ambig = 0.0
        self._total_tokens = 0
        # The grammar root nonterminal to be used
        # for parsing within this job
        self._root = root
        # A progress function to call during processing
        self._progress_func = progress_func
        # The maximum length, in tokens, of a sentence that we will attempt to parse
        self._max_sent_tokens = max_sent_tokens

    def _add_sentence(
        self, s: TokenList, num: int, parse_time: float, reduce_time: float
    ) -> None:
        """ Add a processed sentence to the statistics """
        slen = len(s)
        self._num_sent += 1
        self._num_tokens += slen
        if num > 0:
            # The sentence was parsed successfully
            self._num_parsed += 1
            self._num_combinations += num
            ambig_factor = num ** (1 / slen)
            self._total_ambig += ambig_factor * slen
            self._total_tokens += slen
        # Accumulate the total time spent on parsing and reduction
        self._parse_time += parse_time
        # Accumulate the time thereof spent on reduction
        self._reduce_time += reduce_time
        # Call the progress function, if any
        if self._progress_func is not None:
            assert self._cnt_sent > 0
            assert (self._num_sent + 1) <= self._cnt_sent
            # We add one to the _num_sent counter because we
            # have an additional first step that we call after tokenization
            self._progress_func((self._num_sent + 1) / self._cnt_sent)

    def _create_sentence(self, s: TokenList) -> _Sentence:
        """ Create a fresh _Sentence object """
        return self._r.create_sentence(self, s)

    @property
    def parse_immediately(self) -> bool:
        """ Return True if sentences in the job should be parsed immediately """
        return self._parse

    def paragraphs(self) -> Iterable[_Paragraph]:
        """ Yield the paragraphs from the token stream """
        if self._progress_func is not None:
            # We have a progress function, so we must pre-count
            # the sentences to be processed. This means that all input
            # data must be converted to lists, exhausting generators.
            # Note that the paragraph splitting phase applies the
            # token-level corrections, so they are not really
            # counted in the progress.
            pg_list = [
                [(ix, list(sent)) for ix, sent in p] for p in paragraphs(self._tokens)
            ]
            self._cnt_sent = sum(len(p) for p in pg_list) + 1
            # Make an "first step" initial call to the progress function
            # with a progress of 1, after we've tokenized the input
            self._progress_func(1 / self._cnt_sent)
            plist = iter(pg_list)
        else:
            # No progress function: use generators throughout
            plist = paragraphs(self._tokens)
        for p in plist:
            yield _Paragraph(self, p)

    def sentences(self) -> Iterable[_Sentence]:
        """ Yield the sentences from the token stream """
        for p in self.paragraphs():
            yield from p.sentences()

    def parse(self, tokens: TokenList) -> Tuple[Any, int, int]:
        """ Parse the token sequence, returning a parse tree,
            the number of trees in the parse forest, and the
            score of the best tree """
        num = 0
        score = 0
        forest = None
        t0 = t1 = time.time()
        try:
            if self._max_sent_tokens and len(tokens) > self._max_sent_tokens:
                # Sentence is above the maximum length: don't attempt to parse it
                raise ParseError(
                    "Sentence is longer than {0} tokens".format(self._max_sent_tokens),
                    token_index=self._max_sent_tokens,
                )
            if not self.parse_foreign_sentences and tokens_are_foreign(
                tokens, min_icelandic_ratio=ICELANDIC_RATIO
            ):
                # Sentence is foreign: don't attempt to parse it
                raise ParseError("Sentence is probably not in Icelandic", token_index=0)
            forest = self.parser.go(tokens, root=self._root)
            t1 = time.time()
            if forest is not None:
                num = Fast_Parser.num_combinations(forest)
                if num > 1:
                    # Reduce the parse forest to a single
                    # "best" (highest-scoring) parse tree
                    forest, score = self.reducer.go_with_score(forest)
            return forest, num, score
        finally:
            # Accumulate statistics in the job object
            now = time.time()
            self._add_sentence(tokens, num, parse_time=now - t0, reduce_time=now - t1)

    def __iter__(self) -> Iterator[_Sentence]:
        """ Allow easy iteration of sentences within this job """
        return iter(self.sentences())

    @property
    def parser(self) -> Fast_Parser:
        """ The job's associated parser object """
        return self._parser

    @property
    def reducer(self) -> Reducer:
        """ The job's associated reducer object """
        return self._reducer

    @property
    def num_tokens(self) -> int:
        """ Total number of tokens in sentences submitted to this job """
        return self._num_tokens

    @property
    def num_sentences(self) -> int:
        """ Total number of sentences submitted to this job """
        return self._num_sent

    @property
    def num_parsed(self) -> int:
        """ Total number of sentences successfully parsed within this job """
        return self._num_parsed

    @property
    def num_combinations(self) -> int:
        """ Sum of the total number of parse tree combinations
            for sentences within this job """
        return self._num_combinations

    @property
    def ambiguity(self) -> float:
        """ The weighted average total ambiguity of parsed sentences
            within this job """
        return (
            (self._total_ambig / self._total_tokens) if self._total_tokens > 0 else 1.0
        )

    @property
    def parse_time(self) -> float:
        """ Total time spent on parsing (including reduction) during this job,
            in seconds """
        return self._parse_time

    @property
    def reduce_time(self) -> float:
        """ Total time spent on tree reduction during this job, in seconds """
        return self._reduce_time

    @property
    def parse_foreign_sentences(self) -> bool:
        """ Return True if foreign-looking sentences should be parsed """
        return self._r.parse_foreign_sentences


class _Job_NP(_Job):

    """ Specialized _Job class that creates _NounPhrase objects
        instead of _Sentence objects """

    def __init__(
        self,
        greynir: "Greynir",
        tokens: Iterable[Tok],
        *,
        force_number: Optional[str] = None,
    ) -> None:
        # Parse the tokens with 'Nl' (noun phrase) as the root nonterminal
        # instead of the usual default 'S0' (sentence) root
        root = "Nl"
        if force_number:
            if force_number in ("et", "singular"):
                # We only want singular parses
                root = "Nl_et"
            elif force_number in ("ft", "plural"):
                # We only want plural parses
                root = "Nl_ft"
            else:
                raise ValueError("Unexpected value of force_number")
        super().__init__(greynir, tokens, parse=True, root=root)

    def _create_sentence(self, s: TokenList) -> _NounPhrase:
        """ Create a fresh _NounPhrase object """
        return _NounPhrase(self, s)


class Greynir:

    """ Utility class to tokenize and parse text, organized
        as a sequence of sentences or alternatively as paragraphs
        of sentences. Typical usage:

        g = Greynir()
        job = g.submit(my_text)
        # Iterate through sentences and parse each one:
        for sent in job:
            if sent.parse():
                # sentence parsed successfully
                # do something with sent.tree
                print(sent.tree)
            else:
                # an error occurred in the parse
                # the error token index is at sent.err_index
                pass
        # Alternatively, split into paragraphs first:
        job = g.submit(my_text)
        for p in job.paragraphs(): # Yields paragraphs
            for sent in p.sentences(): # Yields sentences
                if sent.parse():
                    # sentence parsed successfully
                    # do something with sent.tree
                    print(sent.tree)
                else:
                    # an error occurred in the parse
                    # the error token index is at sent.err_index
                    pass
        # After parsing all sentences in a job, the following
        # statistics are available:
        num_sentences = job.num_sentences   # Total number of sentences
        num_parsed = job.num_parsed         # Thereof successfully parsed
        ambiguity = job.ambiguity           # Average ambiguity factor
        parse_time = job.parse_time         # Elapsed time since job was created

    """

    _parser: Optional[Fast_Parser] = None
    _reducer: Optional[Reducer] = None
    _lock = Lock()

    def __init__(self, **options: Any) -> None:
        """ Tokenization options can be passed as keyword arguments to the
            Greynir constructor """
        # Set parse_foreign_sentences to True to attempt to parse
        # all sentences, even if probably foreign
        self._parse_foreign_sentences: bool = options.pop(
            "parse_foreign_sentences", False
        )
        self._options = options

    @property
    def parse_foreign_sentences(self) -> bool:
        """ Return True if the parser should attempt to parse sentences
            that look to be foreign, i.e. not in Icelandic """
        return self._parse_foreign_sentences

    @classmethod
    def _dump_token(cls, tok: Tok) -> Tuple:
        """ Allow derived classes to override how tokens are dumped """
        # Returns (kind, txt, val) - corresponding to
        # the expected signature of _load_token()
        return tuple(tok)

    @classmethod
    def _load_token(cls, *args):
        """ Load token from serialized data """
        return Tok(*load_token(*args))

    def dumps_single(self, sent: _Sentence, **kwargs) -> str:
        """ Return a _Sentence object in a JSON-formatted string,
            which can be loaded again using loads_single() """
        return sent.dumps(self.__class__, **kwargs)

    def loads_single(self, json_str: str, **kwargs) -> _Sentence:
        """ Load previously dumped JSON description of a single sentence.
            Useful for retrieving parsed data from a database. """
        return _Sentence.loads(self.__class__, json_str, **kwargs)

    def tokenize(self, text: StringIterable) -> Iterable[Tok]:
        """ Call the tokenizer (overridable in derived classes) """
        return bin_tokenize(text, **self._options)

    def create_sentence(self, job: _Job, s: TokenList) -> _Sentence:
        """ Override this in derived classes to modify how sentences
            are created or postprocessed """
        return _Sentence(job, s)

    @property
    def parser(self) -> Fast_Parser:
        """ Return the parser instance to be used """
        with self._lock:
            if Greynir._parser is None:
                # Initialize a singleton instance of the parser and the reducer.
                # Both classes are re-entrant and thread safe.
                Greynir._parser = Fast_Parser()
                Greynir._reducer = Reducer(Greynir._parser.grammar)
            return Greynir._parser

    @property
    def reducer(self) -> Reducer:
        """ Return the reducer instance to be used """
        # Should always retrieve the parser attribute first
        assert Greynir._reducer is not None
        return Greynir._reducer

    def submit(
        self,
        text: str,
        parse: bool = False,
        *,
        split_paragraphs: bool = False,
        progress_func: ProgressFunc = None,
        max_sent_tokens: int = DEFAULT_MAX_SENT_TOKENS,
    ) -> _Job:
        """ Submit a text to the tokenizer and parser, yielding a job object.
            The paragraphs and sentences of the text can then be iterated
            through via the job object. If parse is set to True, the
            sentences are automatically parsed before being returned.
            Otherwise, they need to be explicitly parsed by calling
            sent.parse(). This is a more incremental, asynchronous
            approach than Greynir.parse().
            
            If progress_func is given, it will be called during processing
            with a single float parameter between 0.0..1.0 indicating the
            ratio of progress so far with the parsing job. """

        if split_paragraphs:
            # Original text consists of paragraphs separated by newlines:
            # insert paragraph separators before tokenization
            text = mark_paragraphs(text)
        tokens = self.tokenize(text)
        return _Job(
            self,
            tokens,
            parse=parse,
            progress_func=progress_func,
            max_sent_tokens=max_sent_tokens,
        )

    def parse(
        self,
        text: str,
        *,
        progress_func: ProgressFunc = None,
        max_sent_tokens: int = DEFAULT_MAX_SENT_TOKENS,
    ) -> ParseResult:
        """ Convenience function to parse text synchronously and return
            a summary of all contained sentences. The progress_func parameter
            works as described for Greynir.submit(). """
        tokens = self.tokenize(text)
        job = _Job(
            self,
            tokens,
            parse=True,
            progress_func=progress_func,
            max_sent_tokens=max_sent_tokens,
        )
        # Iterating through the sentences in the job causes
        # them to be parsed and their statistics collected
        sentences = [sent for sent in job]
        return dict(
            sentences=sentences,
            num_sentences=job.num_sentences,
            num_parsed=job.num_parsed,
            num_tokens=job.num_tokens,
            ambiguity=job.ambiguity,
            parse_time=job.parse_time,
            reduce_time=job.reduce_time,
        )

    def parse_single(
        self, sentence: str, *, max_sent_tokens: int = DEFAULT_MAX_SENT_TOKENS
    ) -> Optional[_Sentence]:
        """ Convenience function to parse a single sentence only """
        tokens = self.tokenize(sentence)
        job = _Job(self, tokens, parse=True, max_sent_tokens=max_sent_tokens)
        # Returns None if no sentence could be extracted from the text
        try:
            return next(iter(job))
        except StopIteration:
            return None

    def parse_tokens(
        self, tokens: Iterable[Tok], *, max_sent_tokens: int = DEFAULT_MAX_SENT_TOKENS
    ) -> Optional[_Sentence]:
        """ Convenience function to parse a single sentence from tokens """
        job = _Job(self, tokens, parse=True, max_sent_tokens=max_sent_tokens)
        # Returns None if no sentence could be extracted from the text
        try:
            return next(iter(job))
        except StopIteration:
            return None

    def parse_noun_phrase(
        self, noun_phrase: str, *, force_number: Optional[str] = None
    ) -> Optional[_NounPhrase]:
        """ Utility function to parse a noun phrase. Note that in most
            cases it is more convenient to use the NounPhrase class
            for this purpose. """
        # When tokenizing a noun phrase, don't assume that it starts a sentence
        tokens = self.tokenize(noun_phrase)
        # Use a _Job_NP to generate _NounPhrase objects instead of _Sentence objects
        job = _Job_NP(self, tokens, force_number=force_number)
        # Returns None if no noun phrase could be extracted from the text
        try:
            return cast(_NounPhrase, next(iter(job)))
        except StopIteration:
            return None

    def lemmatize(
        self,
        txt: str,
        *,
        all_lemmas: bool = False,
        sortkey: Callable[[LemmaTuple], Comparable] = None,
    ) -> Union[Iterator[LemmaTuple], Iterator[List[LemmaTuple]]]:
        """ Utiility function to (simplistically) lemmatize all words in
            a given string without parsing. Returns a generator of
            (lemma, word category) tuples, one for each text token
            in the input (text tokens being words, person names and entity names).
            Punctuation, dates, numbers, e-mail addresses and other token types
            are skipped and not included in the output.
            If all_lemmas is True, the function returns a list of tuples
            with all possible lemmas for each text token.
            If all_lemmas is True and a sortkey is given, the returned
            list is sorted using that function as a sort key, cf. the
            Python built-in list.sort() function. """
        return simple_lemmatize(txt, all_lemmas=all_lemmas, sortkey=sortkey)

    @classmethod
    def cleanup(cls) -> None:
        """ Discard memory resources held by the Greynir class object """
        cls._reducer = None
        if cls._parser is not None:
            Fast_Parser.discard_grammar()
            cls._parser.cleanup()
            cls._parser = None


# Allow old class name for compatibility
Reynir = Greynir
