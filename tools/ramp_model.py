#!/usr/bin/env python3
"""Characterise the pumpcontrol_ramp controller using its own functions.

Imports stepsize() and find_optimal_pump_volumes() straight out of
evolver_code/custom_script.py, so this cannot drift from the deployed logic.
Requires scipy.

    python3 tools/ramp_model.py
"""
import os, sys, numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
EVOLVER_SRC = os.path.join(os.path.dirname(HERE), "evolver_code", "custom_script.py")
src = open(EVOLVER_SRC).read()
head = src.split("def bye(")[0]           # just the two math functions + imports
ns = {}
exec(compile(head, "custom_script_head", "exec"), ns)
stepsize, fopt = ns["stepsize"], ns["find_optimal_pump_volumes"]

# Defaults match the current experiment; override on the command line if they change.
CL   = float(sys.argv[1]) if len(sys.argv) > 1 else 0.0   # low reservoir g/L
CH   = float(sys.argv[2]) if len(sys.argv) > 2 else 5.0   # high reservoir g/L
RAMP = float(sys.argv[3]) if len(sys.argv) > 3 else 0.05  # target_ramp g/L
D    = 5.0                                                # MAXDISPENSE mL
V    = float(sys.argv[4]) if len(sys.argv) > 4 else 22.0  # vial volume mL
print(f"low={CL} g/L  high={CH} g/L  ramp={RAMP} g/L  vial={V} mL\n")

print("c1 -> achieved step, and high-media fraction of the 10 mL cycle")
print(" c1     vl1    vl2   delta  target  high_frac")
for c1 in [0.0, 0.05, 0.1, 0.25, 0.5, 1.0, 1.43, 2.0, 3.0, 4.0, 4.5, 4.9]:
    vl1, vl2 = fopt(c1, CL, CH, RAMP, maxdispense=D, vialvolume=V)
    d = stepsize(c1, CL, CH, vl1, vl2, D, V)
    hf = ((D-vl1)+(D-vl2)) / (2*D)
    print("%5.2f  %5.2f  %5.2f  %6.3f  %5.2f  %8.3f" % (c1, vl1, vl2, d, RAMP, hf))

# trajectory if the ramp fires every cycle
c, cycles, traj = 0.0, 0, []
while c < 4.99 and cycles < 100000:
    vl1, vl2 = fopt(c, CL, CH, RAMP, maxdispense=D, vialvolume=V)
    d = stepsize(c, CL, CH, vl1, vl2, D, V)
    if d <= 1e-9: break
    c += d; cycles += 1
    traj.append((cycles, c))
print("\nIf the ramp fired on every cycle (fastest possible):")
for target in [0.5, 1.0, 1.43, 2.0, 3.0, 4.0, 4.5]:
    hit = next((n for n,v in traj if v >= target), None)
    print("  reach %4.2f g/L after %s dispense cycles" % (target, hit))
print("  ceiling approached asymptotically; %d cycles to 4.99 g/L" % cycles)

# hold behaviour: what does the controller do when NOT ramping?
print("\nHolding (target_ramp = 0):")
for c1 in [0.0, 0.5, 1.43, 3.0]:
    vl1, vl2 = fopt(c1, CL, CH, 0.0, maxdispense=D, vialvolume=V)
    d = stepsize(c1, CL, CH, vl1, vl2, D, V)
    hf = ((D-vl1)+(D-vl2)) / (2*D)
    print("  c1=%4.2f  vl1=%.2f vl2=%.2f  delta=%+.4f  high_frac=%.3f" % (c1, vl1, vl2, d, hf))
