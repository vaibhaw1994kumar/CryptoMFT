#!/bin/bash
# Order 3 part C: the eight rehearsal runs, with the lifecycle report (--regime-boundaries, as the 13 Nov runs), in parallel at the user's request to accelerate. EXPLORATORY.
cd /Users/vaibhav/Documents/Newproject
D=out/integration/rehearsal_20261117
cat $D/queue.txt | xargs -P 8 -L 1 $D/run_one.sh
echo all > $D/rehearsal.done
