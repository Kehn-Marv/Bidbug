# Bidbug: VelesHack 2026 Challenge 4 Solo Submission

**Project Name:** Bidbug  
**Team Name:** `Bidbug`  
**Participant:** Marvellous Egemonye

Bidbug is a highly resilient, optimizer-driven agent built to solve the CoGNETs Swarm Arena challenge.

## Running the Project
Bidbug is built entirely on the provided template infrastructure. To evaluate:

```bash
cp .env.example .env
# Important: Set your team name to exactly what we used during testing.
sed -i 's/^TEAM_NAME=.*/TEAM_NAME=Bidbug/' .env

make up         # Start the arena and baselines
make agent      # Run the Bidbug agent
make graded     # Run the fully graded/fault-injected scenario
make check      # Run the conformance test
```

## Strategy Write-up
Bidbug operates on one core principle: **treat the battery as a pacing heartbeat, not a static fuel tank**. In a highly volatile, network-fault-prone arena, pre-allocating fixed energy blindly causes an agent to either starve early or hoard unspent charge at the end. 

**The Architecture:**
1. **Adaptive Pacing Controller:** We calculate a sustainable battery drain rate based on our target idle fraction. In the "Endgame Sprint" (the final 15 rounds), Bidbug dynamically shrinks its pacing horizon down to 9.0 and reduces its target reserve. This systematically drains any remaining surplus battery for pure CES utility rather than letting it sit idle.
2. **Recency-Weighted Market Estimation:** We use a recency-weighted estimate (`decay=0.75`) of recent market behaviour to estimate other nodes' bids. This filters out 503/429-induced price spikes, keeping the agent responsive to structural shifts but immune to chaotic noise.
3. **125-Point Real-Time Optimizer:** Every round, we evaluate 125 possible bids: 5 fractional energy tiers and 25 compute/security splits. This allows the agent to search a wider set of energy and compute/security trade-offs each round without missing floors.
4. **Resilient Floor Enforcement:** If the optimizer's mathematically best bid yields a dangerous QoS allocation against our market estimate, Bidbug smoothly redistributes its budget from over-funded resources to guarantee compliance.

**What we learned:**
During our iterative development, we tested highly reactive models that adjusted energy caps aggressively based on immediate round clearing prices. They did not succeed as expected. Some variants over-reacted to market noise, causing erratic bids, while others conserved too much battery and fell behind in overall utility. The eventual direction was to make the internal battery state the dominant pacing signal, rather than the immediate market price. Our biggest revelation was identifying that finishing round 60 with 15% battery leaves massive points on the table. The "Endgame Sprint" logic directly solved this, dramatically raising our utility ceiling without starving the overall swarm or violating QoS floors.

## Final Results 
Bidbug has been evaluated relentlessly in isolated local replays of the hostile Graded Arena.

**Best Verified Local Graded Result:**
- **Score:** `18.823`
- **Rounds:** `54`
- **Missed Rounds:** `0`
- **Floor Violations:** `0`
- **Kappa penalties:** `0.00`

Even in heavily faulted runs where the simulator injected massive 503/429 errors, the agent successfully navigated every eligible round without crashing (scoring `17.693` on 53 active rounds).

**Conformance Test (`make check`):**
- **Functional:** 30.0 / 30
- **Resilience:** 20.0 / 20 (survived 27 injected faults flawlessly)
- `ALL CHECKS PASSED`
