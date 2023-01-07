"""
Comparison engine
"""
import types
import logging
from collections import Counter, defaultdict
from functools import total_ordering
import pysam
import truvari

@total_ordering
class MatchResult():  # pylint: disable=too-many-instance-attributes
    """
    A base/comp match holder
    """
    __slots__ = ["base", "comp", "base_gt", "base_gt_count", "comp_gt", "comp_gt_count",
                 "state", "seqsim", "sizesim", "ovlpct", "sizediff", "st_dist", "ed_dist",
                 "gt_match", "multi", "score", "matid"]

    def __init__(self):
        self.base = None
        self.comp = None
        self.base_gt = None
        self.base_gt_count = 0
        self.comp_gt = None
        self.comp_gt_count = 0
        self.matid = None
        self.seqsim = None
        self.sizesim = None
        self.ovlpct = None
        self.sizediff = None
        self.st_dist = None
        self.ed_dist = None
        self.gt_match = None
        self.state = False
        self.score = None
        self.multi = None

    def calc_score(self):
        """
        Unite the similarity measures and make a score
        """
        if None not in [self.seqsim, self.sizesim, self.ovlpct]:
            self.score = (self.seqsim + self.sizesim + self.ovlpct) / 3.0 * 100

    def __lt__(self, other):
        # Trues are always worth more
        if self.state != other.state:
            return self.state < other.state
        return self.score < other.score

    def __eq__(self, other):
        return self.state == other.state and self.score == other.score

    def __str__(self):
        return f'{self.state} {self.score} ->\n {self.base} {self.comp}'

    def __repr__(self):
        sc = round(self.score, 3) if self.score is not None else None
        return f'<truvari.MatchResult ({self.state} {sc})>'


class Matcher():
    """
    Holds matching parameters. Allows calls to be checked for filtering and matches to be made

    Example
        >>> import pysam
        >>> import truvari
        >>> mat = truvari.Matcher()
        >>> mat.params.pctseq = 0
        >>> v = pysam.VariantFile('repo_utils/test_files/variants/input1.vcf.gz')
        >>> one = next(v); two = next(v)
        >>> mat.build_match(one, two)
        <truvari.bench.MatchResult (False 2.381)>
    """

    def __init__(self, args=None):
        """
        Initalize. args is a Namespace from argparse
        """
        if args is not None:
            self.params = self.make_match_params_from_args(args)
        else:
            self.params = self.make_match_params()

        self.reference = None
        if self.params.reference is not None:
            self.reference = pysam.FastaFile(self.params.reference)

    @staticmethod
    def make_match_params():
        """
        Makes a simple namespace of matching parameters. Holds defaults
        """
        ret = types.SimpleNamespace()
        ret.reference = None
        ret.refdist = 500
        ret.pctseq = 0.70
        ret.minhaplen = 50
        ret.pctsize = 0.70
        ret.pctovl = 0.0
        ret.typeignore = False
        ret.use_lev = False
        ret.chunksize = 1000
        ret.gtcomp = False
        ret.bSample = 0
        ret.cSample = 0
        ret.dup_to_ins = False
        # filtering properties
        ret.sizemin = 50
        ret.sizefilt = 30
        ret.sizemax = 50000
        ret.passonly = False
        ret.no_ref = False
        ret.multimatch = False
        return ret

    @staticmethod
    def make_match_params_from_args(args):
        """
        Makes a simple namespace of matching parameters
        """
        ret = types.SimpleNamespace()
        ret.reference = args.reference
        ret.refdist = args.refdist
        ret.pctseq = args.pctseq
        ret.minhaplen = args.minhaplen
        ret.pctsize = args.pctsize
        ret.pctovl = args.pctovl
        ret.typeignore = args.typeignore
        ret.use_lev = args.use_lev
        ret.chunksize = args.chunksize
        ret.gtcomp = args.gtcomp
        ret.bSample = args.bSample if args.bSample else 0
        ret.cSample = args.cSample if args.cSample else 0
        ret.dup_to_ins = args.dup_to_ins if "dup_to_ins" in args else False
        # filtering properties
        ret.sizemin = args.sizemin
        ret.sizefilt = args.sizefilt
        ret.sizemax = args.sizemax
        ret.passonly = args.passonly
        ret.no_ref = args.no_ref
        ret.multimatch = args.multimatch
        return ret

    def filter_call(self, entry, base=False):
        """
        Returns True if the call should be filtered
        Base has different filtering requirements, so let the method know
        """
        size = truvari.entry_size(entry)
        if size > self.params.sizemax:
            return True

        if base and size < self.params.sizemin:
            return True

        if not base and size < self.params.sizefilt:
            return True

        samp = self.params.bSample if base else self.params.cSample
        prefix = 'b' if base else 'c'
        if (self.params.no_ref in ["a", prefix] or self.params.gtcomp) and not truvari.entry_is_present(entry, samp):
            return True

        if self.params.passonly and truvari.entry_is_filtered(entry):
            return True

        return False

    def build_match(self, base, comp, matid=None, skip_gt=False):
        """
        Build a MatchResult
        """
        ret = MatchResult()
        ret.base = base
        ret.comp = comp

        ret.matid = matid
        ret.state = True

        if not self.params.typeignore and not truvari.entry_same_variant_type(base, comp, self.params.dup_to_ins):
            logging.debug("%s and %s are not the same SVTYPE",
                          str(base), str(comp))
            ret.state = False

        bstart, bend = truvari.entry_boundaries(base)
        cstart, cend = truvari.entry_boundaries(comp)
        if not truvari.overlaps(bstart - self.params.refdist, bend + self.params.refdist, cstart, cend):
            logging.debug("%s and %s are not within REFDIST",
                          str(base), str(comp))
            ret.state = False

        ret.sizesim, ret.sizediff = truvari.entry_size_similarity(base, comp)
        if ret.sizesim < self.params.pctsize:
            logging.debug("%s and %s size similarity is too low (%.3f)",
                          str(base), str(comp), ret.sizesim)
            ret.state = False

        if not skip_gt:
            if "GT" in base.samples[self.params.bSample]:
                ret.base_gt = base.samples[self.params.bSample]["GT"]
                ret.base_gt_count = sum(1 for _ in ret.base_gt if _ == 1)
            if "GT" in comp.samples[self.params.cSample]:
                ret.comp_gt = comp.samples[self.params.cSample]["GT"]
                ret.comp_gt_count = sum(1 for _ in ret.comp_gt if _ == 1)
            ret.gt_match = abs(ret.base_gt_count - ret.comp_gt_count)
            #if self.params.gtcomp and not ret.gt_match:
            #    logging.debug("%s and %s are not the same genotype",
            #                  str(base), str(comp))
            #    ret.state = False

        ret.ovlpct = truvari.entry_reciprocal_overlap(base, comp)
        if ret.ovlpct < self.params.pctovl:
            logging.debug("%s and %s overlap percent is too low (%.3f)",
                          str(base), str(comp), ret.ovlpct)
            ret.state = False

        ret.st_dist, ret.ed_dist = truvari.entry_distance(base, comp)
        if self.params.pctseq > 0:
            ret.seqsim = truvari.entry_seq_similarity(base, comp, self.reference,
                                              self.params.minhaplen, self.params.use_lev)
            if ret.seqsim < self.params.pctseq:
                logging.debug("%s and %s sequence similarity is too low (%.3ff)",
                              str(base), str(comp), ret.seqsim)
                ret.state = False
        else:
            ret.seqsim = 0

        ret.calc_score()

        return ret

############################
# Parsing and set building #
############################
def file_zipper(*start_files):
    """
    Zip files to yield the entries in order.
    Each file must be sorted in the same order.
    start_files is a tuple of ('key', iterable)
    where key is the identifier (so we know which file the yielded entry came from)
    and iterable is usually a pysam.VariantFile

    yields key, pysam.VariantRecord
    """
    next_markers = []
    files = []
    names = []
    file_counts = Counter()
    for name, i in start_files:
        try:
            next_markers.append(next(i))
            names.append(name)
            files.append(i)
        except StopIteration:
            # For when there are no variants in the file
            pass

    while next_markers:
        sidx = 0  # assume the first is the least
        for idx, i in enumerate(next_markers):
            if i.chrom < next_markers[sidx].chrom:
                sidx = idx
            elif i.chrom == next_markers[sidx].chrom and i.start < next_markers[sidx].start:
                sidx = idx
        entry = next_markers[sidx]
        key = names[sidx]
        file_counts[key] += 1
        try:
            next_markers[sidx] = next(files[sidx])
        except StopIteration:
            # This file is done
            files.pop(sidx)
            names.pop(sidx)
            next_markers.pop(sidx)
        yield key, entry
    logging.info("Zipped %d variants %s", sum(file_counts.values()), file_counts)

def chunker(matcher, *files):
    """
    Given a Matcher and multiple files, zip them and create chunks

    Yields tuple of the matcher, the chunk of calls, and how many unfiltered calls in chunk
    """
    call_counts = Counter()
    chunk_count = 0
    cur_chrom = None
    cur_end = None
    cur_chunk = defaultdict(list)
    for key, entry in file_zipper(*files):
        if entry.alts is None: # ignore monomorphic reference
            cur_chunk['__filtered'].append(entry)
            call_counts['__filtered'] += 1
            continue
        new_chrom = cur_chrom and entry.chrom != cur_chrom
        new_chunk = cur_end and cur_end + matcher.params.chunksize < entry.start
        if new_chunk or new_chrom:
            chunk_count += 1
            yield matcher, cur_chunk, chunk_count
            # Reset
            cur_chrom = None
            cur_end = None
            cur_chunk = defaultdict(list)

        if not matcher.filter_call(entry, key == 'base'):
            logging.debug(f"Adding to {key} -> {entry}")
            cur_chrom = entry.chrom
            cur_end = entry.stop
            cur_chunk[key].append(entry)
            call_counts[key] += 1
        else:
            cur_chunk['__filtered'].append(entry)
            call_counts['__filtered'] += 1
    chunk_count += 1
    logging.info("%d chunks of %d variants %s", chunk_count, sum(call_counts.values()), call_counts)
    yield matcher, cur_chunk, chunk_count
