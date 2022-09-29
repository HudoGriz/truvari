# ------------------------------------------------------------
#                                 segment
# ------------------------------------------------------------
run test_segment $truv segment $ANSDIR/anno_answers.vcf.gz $OD/segment.vcf
if [ $test_segment ]; then
    assert_exit_code 0
    run test_segment_result
    assert_equal $(fn_md5 $ANSDIR/segment.vcf) $(fn_md5 $OD/segment.vcf)
fi