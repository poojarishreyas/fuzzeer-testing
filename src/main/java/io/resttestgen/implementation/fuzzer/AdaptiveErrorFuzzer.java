package io.resttestgen.implementation.fuzzer;

import io.resttestgen.core.Environment;
import io.resttestgen.core.openapi.Operation;
import io.resttestgen.core.testing.Fuzzer;
import io.resttestgen.core.testing.TestInteraction;
import io.resttestgen.core.testing.TestRunner;
import io.resttestgen.core.testing.TestSequence;
import io.resttestgen.implementation.mutator.operation.MutateRandomParameterWithParameterMutatorOperationMutator;
import io.resttestgen.implementation.mutator.parameter.ConstraintViolationParameterMutator;
import io.resttestgen.implementation.mutator.parameter.MissingRequiredParameterMutator;
import io.resttestgen.implementation.mutator.parameter.WrongTypeParameterMutator;
import io.resttestgen.implementation.oracle.ErrorStatusCodeOracle;
import io.resttestgen.implementation.writer.ReportWriter;
import io.resttestgen.implementation.writer.RestAssuredWriter;
import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;

import java.io.IOException;
import java.util.*;
import java.util.stream.Collectors;

/**
 * Adaptive error fuzzer that uses Q-learning to prioritize mutations that expose weaknesses.
 *
 * Q-table: per-operation scores for each mutator.
 * Rewards:
 *   4xx (rejected)          → +1.0  (expected — mutation detected)
 *   2xx (accepted bad input) → +10.0 (bug found — re-mutate deeper)
 *   5xx (server crash)       → +5.0  (critical bug found)
 *
 * When a 2xx is returned for a bad request (weakness found), the fuzzer re-mutates
 * that already-broken operation up to MAX_DEPTH times to find further bugs.
 */
public class AdaptiveErrorFuzzer extends Fuzzer {

    private static final Logger logger = LogManager.getLogger(AdaptiveErrorFuzzer.class);

    // Learning rate: how fast Q-values update
    private static final double ALPHA = 0.4;

    // Exploration rate: probability of choosing a random mutator instead of the best one
    private static final double EPSILON = 0.15;

    // Rewards
    private static final double REWARD_REJECTED  = 1.0;   // 4xx: expected
    private static final double REWARD_WEAKNESS  = 10.0;  // 2xx: bug — API accepted bad input
    private static final double REWARD_CRASH     = 5.0;   // 5xx: server crashed

    // How many times to re-mutate a weak operation before stopping
    private static final int MAX_DEPTH = 3;

    private final TestSequence testSequenceToMutate;

    // The three mutators (same as ErrorFuzzer)
    private final List<MutateRandomParameterWithParameterMutatorOperationMutator> mutators;

    // Q-table: operationId -> (mutator -> score)
    // Each operation gets its own Q-table so learning is operation-specific
    private final Map<String, Map<MutateRandomParameterWithParameterMutatorOperationMutator, Double>> qTables;

    public AdaptiveErrorFuzzer(TestSequence testSequenceToMutate) {
        this.testSequenceToMutate = testSequenceToMutate;

        mutators = new ArrayList<>();
        mutators.add(new MutateRandomParameterWithParameterMutatorOperationMutator(new MissingRequiredParameterMutator()));
        mutators.add(new MutateRandomParameterWithParameterMutatorOperationMutator(new WrongTypeParameterMutator()));
        mutators.add(new MutateRandomParameterWithParameterMutatorOperationMutator(new ConstraintViolationParameterMutator()));

        qTables = new HashMap<>();
    }

    public List<TestSequence> generateTestSequences(int numberOfSequences) {
        List<TestSequence> results = new LinkedList<>();

        for (TestInteraction interaction : testSequenceToMutate) {
            Operation originalOperation = interaction.getFuzzedOperation();
            String operationId = originalOperation.getOperationId();

            // Initialize Q-table for this operation if not seen before
            qTables.putIfAbsent(operationId, initQTable());

            for (int j = 0; j < numberOfSequences; j++) {
                MutateRandomParameterWithParameterMutatorOperationMutator mutator =
                        selectMutator(originalOperation);

                if (mutator == null) {
                    logger.warn("No applicable mutations for operation: {}", operationId);
                    break;
                }

                // Apply mutation, fire, learn — with re-mutation loop on weakness
                results.addAll(applyAndLearn(originalOperation, mutator, 0));
            }

            logQTable(operationId);
        }

        return results;
    }

    /**
     * Applies a mutation, fires the request, gets a reward, updates the Q-table.
     * If the API accepts bad input (2xx), re-mutates deeper up to MAX_DEPTH.
     */
    private List<TestSequence> applyAndLearn(Operation operation,
                                             MutateRandomParameterWithParameterMutatorOperationMutator mutator,
                                             int depth) {
        List<TestSequence> results = new LinkedList<>();

        if (depth >= MAX_DEPTH) {
            return results;
        }

        // Apply mutation to a clone of the operation
        Operation mutatedOperation = mutator.mutate(operation);

        TestInteraction testInteraction = new TestInteraction(mutatedOperation);
        testInteraction.addTag("mutated");
        if (depth > 0) {
            testInteraction.addTag("re-mutated-depth-" + depth);
        }

        // Name and run the sequence
        TestSequence seq = new TestSequence(this, testInteraction);
        String name = !mutatedOperation.getOperationId().isEmpty()
                ? mutatedOperation.getOperationId()
                : mutatedOperation.getMethod() + "-" + mutatedOperation.getEndpoint();
        seq.setName(name);
        seq.appendGeneratedAtTimestampToSequenceName();

        TestRunner.getInstance().run(seq);

        // Evaluate with oracle
        ErrorStatusCodeOracle oracle = new ErrorStatusCodeOracle();
        oracle.assertTestSequence(seq);

        // Compute reward from the response
        double reward = computeReward(seq);

        // Update Q-table for this operation
        String operationId = operation.getOperationId();
        Map<MutateRandomParameterWithParameterMutatorOperationMutator, Double> qTable =
                qTables.computeIfAbsent(operationId, k -> initQTable());

        double oldQ = qTable.getOrDefault(mutator, 1.0);
        double newQ = oldQ + ALPHA * (reward - oldQ);
        qTable.put(mutator, newQ);

        logger.info("[AdaptiveErrorFuzzer] op={} mutator={} depth={} status={} reward={} Q: {} -> {}",
                operationId,
                mutator.getParameterMutator().getClass().getSimpleName(),
                depth,
                seq.getLast().getResponseStatusCode().getCode(),
                reward,
                String.format("%.3f", oldQ),
                String.format("%.3f", newQ));

        // Write reports
        try {
            new ReportWriter(seq).write();
            new RestAssuredWriter(seq).write();
        } catch (IOException e) {
            logger.warn("Could not write report to file.");
        }

        results.add(seq);

        // If the API accepted bad input (2xx = weakness found), re-mutate deeper
        if (seq.getLast().getResponseStatusCode().isSuccessful()) {
            logger.warn("[AdaptiveErrorFuzzer] WEAKNESS FOUND on {} at depth {} — re-mutating deeper.",
                    operationId, depth);

            MutateRandomParameterWithParameterMutatorOperationMutator nextMutator =
                    selectMutator(mutatedOperation);

            if (nextMutator != null) {
                results.addAll(applyAndLearn(mutatedOperation, nextMutator, depth + 1));
            }
        }

        return results;
    }

    /**
     * Epsilon-greedy mutator selection using per-operation Q-table.
     * With probability EPSILON, picks randomly (exploration).
     * Otherwise, picks the mutator with the highest Q-value (exploitation).
     */
    private MutateRandomParameterWithParameterMutatorOperationMutator selectMutator(Operation operation) {
        List<MutateRandomParameterWithParameterMutatorOperationMutator> applicable = mutators.stream()
                .filter(m -> m.isOperationMutable(operation))
                .collect(Collectors.toList());

        if (applicable.isEmpty()) {
            return null;
        }

        String operationId = operation.getOperationId();
        Map<MutateRandomParameterWithParameterMutatorOperationMutator, Double> qTable =
                qTables.computeIfAbsent(operationId, k -> initQTable());

        // Explore: random choice
        if (Environment.getInstance().getRandom().nextDouble() < EPSILON) {
            return Environment.getInstance().getRandom().nextElement(applicable).get();
        }

        // Exploit: pick highest Q-value mutator
        return applicable.stream()
                .max(Comparator.comparingDouble(m -> qTable.getOrDefault(m, 1.0)))
                .orElse(applicable.get(0));
    }

    private double computeReward(TestSequence seq) {
        if (seq.isEmpty() || !seq.isExecuted()) {
            return 0.0;
        }
        TestInteraction last = seq.getLast();
        if (last.getResponseStatusCode().isClientError()) {
            return REWARD_REJECTED;   // 4xx — expected, mutation was detected
        } else if (last.getResponseStatusCode().isSuccessful()) {
            return REWARD_WEAKNESS;   // 2xx — bug, API accepted bad input
        } else if (last.getResponseStatusCode().isServerError()) {
            return REWARD_CRASH;      // 5xx — crash, critical bug
        }
        return 0.0;
    }

    private Map<MutateRandomParameterWithParameterMutatorOperationMutator, Double> initQTable() {
        Map<MutateRandomParameterWithParameterMutatorOperationMutator, Double> table = new IdentityHashMap<>();
        for (MutateRandomParameterWithParameterMutatorOperationMutator m : mutators) {
            table.put(m, 1.0);
        }
        return table;
    }

    private void logQTable(String operationId) {
        Map<MutateRandomParameterWithParameterMutatorOperationMutator, Double> qTable = qTables.get(operationId);
        if (qTable == null) return;
        logger.info("[AdaptiveErrorFuzzer] Q-table for '{}':", operationId);
        qTable.forEach((m, q) ->
                logger.info("  {} -> {}", m.getParameterMutator().getClass().getSimpleName(),
                        String.format("%.3f", q)));
    }
}
