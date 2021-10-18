"""
Structural variant caller comparison tool
Given a benchmark and callset, calculate the recall/precision/f-measure
"""
# pylint: disable=too-many-statements
import os
import sys
import json
import logging
import argparse
from functools import cmp_to_key
from collections import defaultdict, OrderedDict, namedtuple

import pysam
from intervaltree import IntervalTree

import truvari
from truvari.giab_report import make_giabreport

MATCHRESULT = namedtuple("matchresult", ("score seq_similarity size_similarity "
                                         "ovl_pct size_diff start_distance "
                                         "end_distance match_entry"))


class StatsBox(OrderedDict):
    """
    Make a blank stats box for counting TP/FP/FN and calculating performance
    """

    def __init__(self):
        super().__init__()
        self["TP-base"] = 0
        self["TP-call"] = 0
        self["FP"] = 0
        self["FN"] = 0
        self["precision"] = 0
        self["recall"] = 0
        self["f1"] = 0
        self["base cnt"] = 0
        self["call cnt"] = 0
        self["TP-call_TP-gt"] = 0
        self["TP-call_FP-gt"] = 0
        self["TP-base_TP-gt"] = 0
        self["TP-base_FP-gt"] = 0
        self["gt_concordance"] = 0

    def calc_performance(self, peek=False):
        """
        Calculate the precision/recall
        """
        do_stats_math = True
        if self["TP-base"] == 0 and self["FN"] == 0:
            logging.warning("No TP or FN calls in base!")
            do_stats_math = False
        elif self["TP-call"] == 0 and self["FP"] == 0:
            logging.warning("No TP or FP calls in comp!")
            do_stats_math = False
        elif peek:
            logging.info("Results peek: %d TP-base %d FN %.2f%% Recall", self["TP-base"], self["FN"],
                         100 * (float(self["TP-base"]) / (self["TP-base"] + self["FN"])))
        if peek:
            return

        # Final calculations
        if do_stats_math:
            self["precision"] = float(
                self["TP-call"]) / (self["TP-call"] + self["FP"])
            self["recall"] = float(self["TP-base"]) / \
                (self["TP-base"] + self["FN"])
            if self["TP-call_TP-gt"] + self["TP-call_FP-gt"] != 0:
                self["gt_concordance"] = float(self["TP-call_TP-gt"]) / (self["TP-call_TP-gt"] +
                                                                         self["TP-call_FP-gt"])

        # f-measure
        neum = self["recall"] * self["precision"]
        denom = self["recall"] + self["precision"]
        if denom != 0:
            self["f1"] = 2 * (neum / denom)
        else:
            self["f1"] = "NaN"


def parse_args(args):
    """
    Pull the command line parameters
    """
    parser = argparse.ArgumentParser(prog="bench", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-b", "--base", type=str, required=True,
                        help="Baseline truth-set calls")
    parser.add_argument("-c", "--comp", type=str, required=True,
                        help="Comparison set of calls")
    parser.add_argument("-o", "--output", type=str, required=True,
                        help="Output directory")
    parser.add_argument("-f", "--reference", type=str, default=None,
                        help="Indexed fasta used to call variants")
    parser.add_argument("--giabreport", action="store_true",
                        help="Parse output TPs/FNs for GIAB annotations and create a report")
    parser.add_argument("--debug", action="store_true", default=False,
                        help="Verbose logging")
    parser.add_argument("--prog", action="store_true",
                        help="Turn on progress monitoring")

    thresg = parser.add_argument_group("Comparison Threshold Arguments")
    thresg.add_argument("-r", "--refdist", type=int, default=500,
                        help="Max reference location distance (%(default)s)")
    thresg.add_argument("-p", "--pctsim", type=truvari.restricted_float, default=0.70,
                        help="Min percent allele sequence similarity. Set to 0 to ignore. (%(default)s)")
    thresg.add_argument("-B", "--buffer", type=truvari.restricted_float, default=0.10,
                        help="Percent of the reference span to buffer the haplotype sequence created")
    thresg.add_argument("-P", "--pctsize", type=truvari.restricted_float, default=0.70,
                        help="Min pct allele size similarity (minvarsize/maxvarsize) (%(default)s)")
    thresg.add_argument("-O", "--pctovl", type=truvari.restricted_float, default=0.0,
                        help="Min pct reciprocal overlap (%(default)s) for DEL events")
    thresg.add_argument("-t", "--typeignore", action="store_true", default=False,
                        help="Variant types don't need to match to compare (%(default)s)")
    thresg.add_argument("--use-lev", action="store_true",
                        help="Use the Levenshtein distance ratio instead of edlib editDistance ratio (%(default)s)")

    genoty = parser.add_argument_group("Genotype Comparison Arguments")
    genoty.add_argument("--gtcomp", action="store_true", default=False,
                        help="Compare GenoTypes of matching calls")
    genoty.add_argument("--bSample", type=str, default=None,
                        help="Baseline calls sample to use (first)")
    genoty.add_argument("--cSample", type=str, default=None,
                        help="Comparison calls sample to use (first)")

    filteg = parser.add_argument_group("Filtering Arguments")
    filteg.add_argument("-s", "--sizemin", type=int, default=50,
                        help="Minimum variant size to consider for comparison (%(default)s)")
    filteg.add_argument("-S", "--sizefilt", type=int, default=30,
                        help="Minimum variant size to load into IntervalTree (%(default)s)")
    filteg.add_argument("--sizemax", type=int, default=50000,
                        help="Maximum variant size to consider for comparison (%(default)s)")
    filteg.add_argument("--passonly", action="store_true", default=False,
                        help="Only consider calls with FILTER == PASS")
    filteg.add_argument("--no-ref", default=False, choices=['a', 'b', 'c'],
                        help="Don't include 0/0 or ./. GT calls from all (a), base (b), or comp (c) vcfs (%(default)s)")
    filteg.add_argument("--includebed", type=str, default=None,
                        help="Bed file of regions in the genome to include only calls overlapping")
    filteg.add_argument("--multimatch", action="store_true", default=False,
                        help=("Allow base calls to match multiple comparison calls, and vice versa. "
                              "Output vcfs will have redundant entries. (%(default)s)"))

    args = parser.parse_args(args)
    if args.pctsim != 0 and not args.reference:
        parser.error("--reference is required when --pctsim is set")

    return args


def match_sorter(candidates):
    """
    Sort a list of MATCHRESULT tuples inplace.

    :param `candidates`: list of MATCHRESULT named tuples
    :type `candidates`: list
    """
    if len(candidates) == 0:
        return
    entry_idx = len(candidates[0]) - 1

    def sort_cmp(mat1, mat2):
        """
        Sort by attributes and then deterministically by hash(str(VariantRecord))
        """
        for i in range(entry_idx):
            if mat1[i] != mat2[i]:
                return mat1[i] - mat2[i]
        return hash(str(mat1[entry_idx])) - hash(str(mat2[entry_idx]))

    candidates.sort(reverse=True, key=cmp_to_key(sort_cmp))


def edit_header(my_vcf):
    """
    Add INFO for new fields to vcf
    #Probably want to put in the PG whatever, too
    """
    # Update header
    # Edit Header
    header = my_vcf.header.copy()
    header.add_line(('##INFO=<ID=TruScore,Number=1,Type=Integer,'
                     'Description="Truvari score for similarity of match">'))
    header.add_line(('##INFO=<ID=PctSeqSimilarity,Number=1,Type=Float,'
                     'Description="Pct sequence similarity between this variant and its closest match">'))
    header.add_line(('##INFO=<ID=PctSizeSimilarity,Number=1,Type=Float,'
                     'Description="Pct size similarity between this variant and its closest match">'))
    header.add_line(('##INFO=<ID=PctRecOverlap,Number=1,Type=Float,'
                     'Description="Percent reciprocal overlap percent of the two calls\' coordinates">'))
    header.add_line(('##INFO=<ID=StartDistance,Number=1,Type=Integer,'
                     'Description="Distance of this call\'s start from comparison call\'s start">'))
    header.add_line(('##INFO=<ID=EndDistance,Number=1,Type=Integer,'
                     'Description="Distance of this call\'s start from comparison call\'s start">'))
    header.add_line(('##INFO=<ID=SizeDiff,Number=1,Type=Float,'
                     'Description="Difference in size(basecall) and size(evalcall)">'))
    header.add_line(('##INFO=<ID=NumNeighbors,Number=1,Type=Integer,'
                     'Description="Number of calls in B that were in the neighborhood (REFDIST) of this call">'))
    header.add_line(('##INFO=<ID=NumThresholdNeighbors,Number=1,Type=Integer,'
                     'Description="Number of calls in B that are within threshold distances of this call">'))
    header.add_line(('##INFO=<ID=MatchId,Number=1,Type=Integer,'
                     'Description="Truvari uid to help tie tp-base.vcf and tp-call.vcf entries together">'))
    return header


def annotate_tp(entry, match_result):
    """
    Add the matching annotations to a vcf entry
    match_score, match_pctsim, match_pctsize, match_ovlpct, match_szdiff, \
                    match_stdist, match_endist, match_entry
    """
    entry.info["PctSeqSimilarity"] = match_result.seq_similarity
    entry.info["PctSizeSimilarity"] = match_result.size_similarity
    entry.info["PctRecOverlap"] = match_result.ovl_pct
    entry.info["SizeDiff"] = match_result.size_diff
    entry.info["StartDistance"] = match_result.start_distance
    entry.info["EndDistance"] = match_result.end_distance


def check_params(args):
    """
    Checks parameters as much as possible.
    All errors are written to stderr without logging since failures mean no output
    """
    check_fail = False
    if os.path.isdir(args.output):
        logging.error("Output directory '%s' already exists", args.output)
        check_fail = True
    if not os.path.exists(args.comp):
        check_fail = True
        logging.error("File %s does not exist", args.comp)
    if not os.path.exists(args.base):
        check_fail = True
        logging.error("File %s does not exist", args.base)
    if not args.comp.endswith(".gz"):
        check_fail = True
        logging.error(
            "Comparison vcf %s does not end with .gz. Must be bgzip'd", args.comp)
    if not os.path.exists(args.comp + '.tbi'):
        check_fail = True
        logging.error(
            "Comparison vcf index %s.tbi does not exist. Must be indexed", args.comp)
    if not args.base.endswith(".gz"):
        check_fail = True
        logging.error(
            "Base vcf %s does not end with .gz. Must be bgzip'd", args.base)
    if not os.path.exists(args.base + '.tbi'):
        check_fail = True
        logging.error(
            "Base vcf index %s.tbi does not exist. Must be indexed", args.base)

    return check_fail


def check_sample(vcf_fn, sampleId=None):
    """
    Return the given sampleId from the var_file
    if sampleId is None, return the first sample
    if there is no first sample in the var_file, raise an error
    """
    vcf_file = pysam.VariantFile(vcf_fn)
    check_fail = False
    if sampleId is not None and sampleId not in vcf_file.header.samples:
        logging.error("Sample %s not found in vcf (%s)", sampleId, vcf_fn)
        check_fail = True
    if len(vcf_file.header.samples) == 0:
        logging.error("No SAMPLE columns found in vcf (%s)", vcf_fn)
        check_fail = True
    return check_fail


def check_inputs(args):
    """
    Checks the inputs against the arguments as much as possible before creating any output
    Returns:
        vcf_bse
    """
    check_fail = False
    check_fail = check_sample(args.base, args.bSample)
    check_fail = check_sample(args.comp, args.cSample)
    return check_fail


def setup_outputs(args):
    """
    Makes all of the output files
    return a ... to get to each of the
    """
    os.mkdir(args.output)
    truvari.setup_logging(args.debug, truvari.LogFileStderr(
        os.path.join(args.output, "log.txt")))
    logging.info("Params:\n%s", json.dumps(vars(args), indent=4))
    logging.info(f"Truvari version: {truvari.__version__}")
    outputs = {}

    outputs["vcf_base"] = pysam.VariantFile(args.base)
    outputs["n_base_header"] = edit_header(outputs["vcf_base"])
    outputs["sampleBase"] = args.bSample if args.bSample else outputs["vcf_base"].header.samples[0]

    outputs["vcf_comp"] = pysam.VariantFile(args.comp)
    outputs["n_comp_header"] = edit_header(outputs["vcf_comp"])
    outputs["sampleComp"] = args.cSample if args.cSample else outputs["vcf_comp"].header.samples[0]

    # Setup outputs
    outputs["tpb_out"] = pysam.VariantFile(os.path.join(
        args.output, "tp-base.vcf"), 'w', header=outputs["n_base_header"])
    outputs["tpc_out"] = pysam.VariantFile(os.path.join(
        args.output, "tp-call.vcf"), 'w', header=outputs["n_comp_header"])

    outputs["fn_out"] = pysam.VariantFile(os.path.join(
        args.output, "fn.vcf"), 'w', header=outputs["n_base_header"])
    outputs["fp_out"] = pysam.VariantFile(os.path.join(
        args.output, "fp.vcf"), 'w', header=outputs["n_comp_header"])

    outputs["stats_box"] = StatsBox()

    return outputs


def filter_call(entry, sizeA, sizemin, sizemax, no_ref, passonly, outputs, base=True):
    """
    Given an entry, the parse_args arguments, and the entry's size
    check if it should be excluded from further analysis
    """
    prefix = "base " if base else "call "
    if sizeA < sizemin or sizeA > sizemax:
        return True

    samp = outputs["sampleBase"] if base else outputs["sampleComp"]
    if no_ref in ["a", prefix[0]] and not truvari.entry_is_present(entry, samp):
        return True

    if passonly and truvari.entry_is_filtered(entry):
        logging.debug("%s variant has no PASS FILTER and is being excluded from comparison - %s",
                      prefix, entry)
        return True
    return False


def write_fn(base_entry, outputs):
    """
    Write an entry to the false_negative output
    """
    # No overlaps, don't even bother checking
    n_base_entry = truvari.copy_entry(base_entry, outputs["n_base_header"])
    n_base_entry.info["NumNeighbors"] = 0
    n_base_entry.info["NumThresholdNeighbors"] = 0
    outputs["stats_box"]["FN"] += 1
    outputs["fn_out"].write(n_base_entry)


def match_calls(base_entry, comp_entry, astart, aend, sizeA, sizeB, regions, reference, args, outputs):  # pylint: disable=too-many-return-statements
    """
    Compare the base and comp entries.
    We provied astart...sizeA because we've presumably calculated it before
    Note - This is the crucial component of matching.. so needs to be better
    pulled apart for reusability and put into comparisons
    """
    if sizeB < args.sizefilt:
        return False

    # Double ensure OVERLAP - there's a weird edge case where fetch with
    # the interval tree can return non-overlaps
    bstart, bend = truvari.entry_boundaries(comp_entry)
    if not truvari.overlaps(astart - args.refdist, aend + args.refdist, bstart, bend):
        return False

    if not regions.include(comp_entry):
        return False

    # Someone in the Base call's neighborhood, we'll see if it passes comparisons

    if args.no_ref in ["a", "c"] and not truvari.entry_is_present(comp_entry, outputs["sampleComp"]):
        logging.debug("%s is uncalled", comp_entry)
        return True

    if args.gtcomp and not truvari.entry_gt_comp(base_entry, comp_entry, outputs["sampleBase"], outputs["sampleComp"]):
        logging.debug("%s and %s are not the same genotype",
                      str(base_entry), str(comp_entry))
        return True

    if not args.typeignore and not truvari.entry_same_variant_type(base_entry, comp_entry):
        logging.debug("%s and %s are not the same SVTYPE",
                      str(base_entry), str(comp_entry))
        return True

    size_similarity, size_diff = truvari.sizesim(sizeA, sizeB)
    if size_similarity < args.pctsize:
        logging.debug("%s and %s size similarity is too low (%f)", str(base_entry),
                      str(comp_entry), size_similarity)
        return True

    ovl_pct = truvari.reciprocal_overlap(astart, aend, bstart, bend)
    if truvari.entry_variant_type(base_entry) == "DEL" and ovl_pct < args.pctovl:
        logging.debug("%s and %s overlap percent is too low (%f)",
                      str(base_entry), str(comp_entry), ovl_pct)
        return True

    if args.pctsim > 0:
        seq_similarity = truvari.entry_pctsim(
            base_entry, comp_entry, reference, args.buffer, args.use_lev)
        if seq_similarity < args.pctsim:
            logging.debug("%s and %s sequence similarity is too low (%f)", str(
                base_entry), str(comp_entry), seq_similarity)
            return True
    else:
        seq_similarity = 0

    start_distance = astart - bstart
    end_distance = aend - bend

    score = truvari.weighted_score(seq_similarity, size_similarity, ovl_pct)

    return MATCHRESULT(score, seq_similarity, size_similarity, ovl_pct, size_diff,
                       start_distance, end_distance, comp_entry)


def output_base_match(base_entry, num_neighbors, thresh_neighbors, myid, matched_calls, outputs):
    """
    Writes a base call after it has gone through matching
    """
    base_entry = truvari.copy_entry(base_entry, outputs["n_base_header"])
    base_entry.info["NumNeighbors"] = num_neighbors
    base_entry.info["NumThresholdNeighbors"] = len(thresh_neighbors)
    base_entry.info["MatchId"] = myid

    if len(thresh_neighbors) == 0:
        # False negative
        outputs["stats_box"]["FN"] += 1
        outputs["fn_out"].write(base_entry)
        return

    logging.debug("Picking from candidate matches:\n%s",
                  "\n".join([str(x) for x in thresh_neighbors]))
    truvari.match_sorter(thresh_neighbors)
    logging.debug("Best match is %s", str(thresh_neighbors[0].score))
    base_entry.info["TruScore"] = thresh_neighbors[0].score

    annotate_tp(base_entry, thresh_neighbors[0])
    outputs["tpb_out"].write(base_entry)

    # Don't double count calls found before
    b_key = truvari.entry_to_key(base_entry, prefix='b', bounds=True)
    if not matched_calls[b_key]:
        # Interesting...
        outputs["stats_box"]["TP-base"] += 1
        if truvari.entry_gt_comp(base_entry, thresh_neighbors[0].match_entry, outputs["sampleBase"], outputs["sampleComp"]):
            outputs["stats_box"]["TP-base_TP-gt"] += 1
        else:
            outputs["stats_box"]["TP-base_FP-gt"] += 1
    # Mark the call for multimatch checking
    matched_calls[b_key] = True


def report_best_match(base_entry, num_neighbors, thresh_neighbors, myid, matched_calls, outputs, args):
    """
    Pick and record the best base_entry
    """
    output_base_match(base_entry, num_neighbors,
                      thresh_neighbors, myid, matched_calls, outputs)

    # Work through the comp calls
    for neigh in thresh_neighbors:
        # Multimatch checking
        c_key = truvari.entry_to_key(
            neigh.match_entry, prefix='c', bounds=True)
        if not matched_calls[c_key]:  # unmatched
            outputs["stats_box"]["TP-call"] += 1
            if truvari.entry_gt_comp(base_entry, neigh.match_entry, outputs["sampleBase"], outputs["sampleComp"]):
                outputs["stats_box"]["TP-call_TP-gt"] += 1
            else:
                outputs["stats_box"]["TP-call_FP-gt"] += 1
        elif not args.multimatch:
            # Used this one and it can't multimatch
            continue

        logging.debug("Matching %s and %s", str(
            base_entry), str(neigh.match_entry))
        match_entry = truvari.copy_entry(
            neigh.match_entry, outputs["n_comp_header"])
        match_entry.info["TruScore"] = neigh.score
        match_entry.info["NumNeighbors"] = num_neighbors
        match_entry.info["NumThresholdNeighbors"] = len(thresh_neighbors)
        match_entry.info["MatchId"] = myid
        annotate_tp(match_entry, neigh)
        outputs["tpc_out"].write(match_entry)

        # Mark the call for multimatch checking
        matched_calls[c_key] = True
        if not args.multimatch:  # We're done here
            break


def parse_fps(matched_calls, tot_comp_entries, regions, args, outputs):
    """
    Report all the false-positives and comp filered calls
    """
    if args.prog:
        pbar = truvari.setup_progressbar(tot_comp_entries)

    # Reset
    vcf_comp = pysam.VariantFile(args.comp)
    for cnt, entry in enumerate(regions.iterate(vcf_comp)):
        # Here
        if args.prog:
            pbar.update(cnt + 1)

        size = truvari.entry_size(entry)
        if filter_call(entry, size, args.sizemin, args.sizemax, args.no_ref, args.passonly, outputs, False):
            continue

        if matched_calls[truvari.entry_to_key(entry, prefix='c', bounds=True)]:
            continue

        if regions.include(entry):
            outputs["fp_out"].write(truvari.copy_entry(
                entry, outputs["n_comp_header"]))
            outputs["stats_box"]["FP"] += 1

    if args.prog:
        pbar.finish()


def close_outputs(outputs):
    """
    Close all the files
    """
    outputs["tpb_out"].close()
    outputs["tpc_out"].close()
    outputs["fn_out"].close()
    outputs["fp_out"].close()


def make_interval_tree(vcf_file, sizemin=10, sizemax=100000, passonly=False):
    """
    Build a dictionary of IntervalTree for intersection querying along with
    how many entries there are total in the vcf_file and how many entries pass
    filtering parameters in vcf_files

    :param `vcf_file`: Filename of VCF to parse
    :type `vcf_file`: string
    :param `sizemin`: Minimum size of event to add to trees
    :type `sizemin`: int, optional
    :param `sizemax`: Maximum size of event to add to trees
    :type `sizemax`: int, optional
    :param `passonly`: Only add PASS variants
    :type `passonly`: boolean, optional

    :return: dictonary of IntervalTrees
    :rtype: dict
    """
    n_entries = 0
    cmp_entries = 0
    lookup = defaultdict(IntervalTree)
    try:
        for entry in vcf_file:
            n_entries += 1
            if passonly and "PASS" not in entry.filter:
                continue
            start, end = truvari.entry_boundaries(entry)
            sz = truvari.entry_size(entry)
            if sz < sizemin or sz > sizemax:
                continue
            cmp_entries += 1
            lookup[entry.chrom].addi(start, end, entry.start)
    except ValueError as e:
        logging.error(
            "Unable to parse comparison vcf file. Please check header definitions")
        logging.error("Specific error: \"%s\"", str(e))
        sys.exit(100)

    return lookup, n_entries, cmp_entries


def fetch_coords(lookup, entry, dist=0):
    """
    Get the minimum/maximum fetch coordinates to find all variants within dist of variant

    :param `lookup`: genome tree build defaultdict with interevaltrees
    :type `lookup`: dict
    :param `entry`: entry to build coords from
    :type `entry`: :class:`pysam.VariantRecord`
    :param `dist`: distance buffer to add/subtract from the coords
    :type `dist`: integer

    :return: the minimum/maximum fetch coordinates for the entry
    :rtype: tuple (int, int)
    """
    start, end = truvari.entry_boundaries(entry)
    start -= dist
    end += dist
    # Membership queries are fastest O(1)
    if not lookup[entry.chrom].overlaps(start, end):
        return None, None

    cand_intervals = lookup[entry.chrom].overlap(start, end)
    s_ret = min(
        [x.data for x in cand_intervals if truvari.overlaps(start, end, x[0], x[1])])
    e_ret = max(
        [x.data for x in cand_intervals if truvari.overlaps(start, end, x[0], x[1])])
    return s_ret, e_ret


def bench_main(cmdargs):  # pylint: disable=too-many-locals
    """
    Main entry point for running Truvari Benchmarking
    """
    args = parse_args(cmdargs)

    if check_params(args) or check_inputs(args):
        sys.stderr.write("Couldn't run Truvari. Please fix parameters\n")
        sys.exit(100)

    # We can now 'safely' perform everything
    outputs = setup_outputs(args)
    reference = pysam.FastaFile(args.reference) if args.reference else None

    logging.info("Creating call interval tree for overlap search")
    regions = truvari.GenomeTree(
        outputs["vcf_base"], outputs["vcf_comp"], args.includebed, args.sizemax)
    span_lookup, tot_comp_entries, cmp_entries = make_interval_tree(
        regions.iterate(outputs["vcf_comp"]), args.sizefilt, args.sizemax, args.passonly)
    logging.info("%d call variants in total", tot_comp_entries)
    logging.info("%d call variants within size range (%d, %d)",
                 cmp_entries, args.sizefilt, args.sizemax)

    num_entries = 0
    pbar = None
    if args.prog:
        for _ in regions.iterate(outputs["vcf_base"]):
            num_entries += 1
        logging.info("%s base variants", num_entries)
        pbar = truvari.setup_progressbar(num_entries)

    # Reset
    outputs["vcf_base"] = pysam.VariantFile(args.base, 'r')
    outputs["n_base_header"] = edit_header(outputs["vcf_base"])
    outputs["vcf_comp"] = pysam.VariantFile(args.comp)
    outputs["n_comp_header"] = edit_header(outputs["vcf_comp"])

    # Calls that have been matched up
    matched_calls = defaultdict(bool)

    # for variant in base - do filtering on it and then try to match it to comp
    logging.info("Matching base to calls")
    for pbarcnt, base_entry in enumerate(regions.iterate(outputs["vcf_base"])):
        if args.prog:
            pbar.update(pbarcnt)

        sizeA = truvari.entry_size(base_entry)

        if filter_call(base_entry, sizeA, args.sizemin, args.sizemax, args.no_ref, args.passonly, outputs, True):
            continue

        outputs["stats_box"]["base cnt"] += 1

        fetch_start, fetch_end = fetch_coords(
            span_lookup, base_entry, args.refdist)
        # No overlaps, don't even bother checking
        if fetch_start is None and fetch_end is None:
            write_fn(base_entry, outputs)
            continue

        # IntervalTree can give boundaries past REFDIST in the case of Inversions where start>end
        # We still need to fetch on the expanded boundaries so we can test them, but
        # we need to filter calls that otherwise shouldn't be considered
        # see the bstart/bend below
        astart, aend = truvari.entry_boundaries(base_entry)

        # Search for comparison vcf entries as potential matches
        thresh_neighbors = []
        num_neighbors = 0

        # +- 1 just to be safe because why not
        for comp_entry in outputs["vcf_comp"].fetch(base_entry.chrom, max(0, fetch_start - 1), fetch_end + 1):
            sizeB = truvari.entry_size(comp_entry)
            if filter_call(comp_entry, sizeB, args.sizefilt, args.sizemax, args.no_ref, args.passonly, outputs, False):
                continue

            # There is a race condition here that could potentially mismatch things
            # If base1 passes matching call1 and then base2 passes matching call1
            # better, it can't use it and we mismatch
            # UPDATE: by default we don't enforce one-match
            logging.debug("Comparing %s %s", str(base_entry), str(comp_entry))
            if not args.multimatch and matched_calls[truvari.entry_to_key(comp_entry, prefix='c', bounds=True)]:
                logging.debug(
                    "No match because comparison call already matched")
                continue
            mat = match_calls(base_entry, comp_entry, astart, aend, sizeA, sizeB, regions,
                              reference, args, outputs)
            if mat:
                num_neighbors += 1
            else:
                continue
            if isinstance(mat, bool):
                continue
            thresh_neighbors.append(mat)

        # Finished with this base entry
        report_best_match(base_entry, num_neighbors,
                          thresh_neighbors, pbarcnt, matched_calls, outputs, args)

    if args.prog:
        pbar.finish()

    outputs["stats_box"].calc_performance(True)

    logging.info("Parsing FPs from calls")
    parse_fps(matched_calls, tot_comp_entries, regions, args, outputs)

    # call count is just of those used were used
    outputs["stats_box"]["call cnt"] = outputs["stats_box"]["TP-base"] + \
        outputs["stats_box"]["FP"]

    # Close to flush vcfs
    close_outputs(outputs)

    # make stats
    outputs["stats_box"].calc_performance(False)
    with open(os.path.join(args.output, "summary.txt"), 'w') as fout:
        fout.write(json.dumps(outputs["stats_box"], indent=4))
        logging.info("Stats: %s", json.dumps(outputs["stats_box"], indent=4))

    if args.giabreport:
        make_giabreport(args, outputs["stats_box"])

    logging.info("Finished bench")