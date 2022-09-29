# ------------------------------------------------------------
#                                 bench
# ------------------------------------------------------------
bench() {
    # run and test truvari bench
    f1=$1
    f2=$2
    k=$3
    rm -rf $OD/bench${k}
    $truv bench -b $INDIR/input${f1}.vcf.gz \
                -c $INDIR/input${f2}.vcf.gz \
                -f $INDIR/reference.fa \
                -o $OD/bench${k}/ ${4}
}

bench_assert() {
    k=$1
    if [ $name ]; then
        assert_exit_code 0
        for i in $ANSDIR/bench${k}/*.vcf
        do
            bname=$(basename $i | sed 's/[\.|\-]/_/g')
            result=$OD/bench${k}/$(basename $i)
            sort_vcf $result
            run test_bench${k}_${bname}
            assert_equal $(fn_md5 $i) $(fn_md5 $result)
        done
    fi
}

run test_bench_12 bench 1 2 12
if [ $test_bench_12 ]; then
    bench_assert  12
fi

run test_bench_13 bench 1 3 13
if [ $test_bench_13 ]; then
    bench_assert 13
fi

run test_bench_23 bench 2 3 23 --multimatch
if [ $test_bench_23 ]; then
    bench_assert 23
fi

# Testing --includebed
run test_bench_13_includebed bench 1 3 13_includebed "--includebed $INDIR/include.bed"
if [ $test_bench_13_includebed ]; then
    bench_assert 13_includebed
fi

# Testing --extend
run test_bench_13_extend bench 1 3 13_extend "--includebed $INDIR/include.bed --extend 500"
if [ $test_bench_13_extend ]; then
    bench_assert 13_extend
fi


# --unroll
rm -rf $OD/bench_unroll
run test_bench_unroll $truv bench -b $INDIR/real_small_base.vcf.gz \
                                  -c $INDIR/real_small_comp.vcf.gz \
                                  -o $OD/bench_unroll/
if [ $test_bench_unroll ]; then
    assert_exit_code 0
    for i in $ANSDIR/bench_unroll/*.vcf
    do
        bname=$(basename $i | sed 's/[\.|\-]/_/g')
        result=$OD/bench_unroll/$(basename $i)
        sort_vcf $result
        run test_bench_unroll_${bname}
        assert_equal $(fn_md5 $i) $(fn_md5 $result)
    done
fi

# --giabreport
rm -rf $OD/bench_giab
run test_bench_giab $truv bench -b $INDIR/giab.vcf.gz \
                                -c $INDIR/input1.vcf.gz \
                                -f $INDIR/reference.fa \
                                -o $OD/bench_giab/ \
                                --includebed $INDIR/giab.bed \
                                --multimatch \
                                --giabreport \
                                --prog
if [ $test_bench_giab ]; then
    assert_exit_code 0
fi

run test_bench_giab_report
if [ $test_bench_giab_report ]; then
    assert_equal $(fn_md5 $ANSDIR/bench_giab_report.txt) $(fn_md5 $OD/bench_giab/giab_report.txt)
fi

run test_bench_badparams $truv bench -b nofile.vcf -c nofile.aga -f notref.fa -o $OD
if [ $test_bench_badparams ]; then
    assert_exit_code 100
fi