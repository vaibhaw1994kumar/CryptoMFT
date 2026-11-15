#!/bin/bash
# the rest of the T1 queue (capture and 03-01, 03-02 were already running/done), four at a time
cd /Users/vaibhav/Documents/Newproject
D=out/integration/cost
xargs -P 4 -I CMD bash -c 'CMD' < $D/followup_t1_jobs2.txt
echo done > $D/t1_followup_rest.done
