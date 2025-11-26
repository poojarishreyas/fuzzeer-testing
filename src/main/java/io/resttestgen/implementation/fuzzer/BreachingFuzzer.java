package io.resttestgen.implementation.fuzzer;

import io.resttestgen.core.Environment;
import io.resttestgen.core.openapi.Operation;
import io.resttestgen.core.testing.Fuzzer;
import io.resttestgen.core.testing.TestInteraction;
import io.resttestgen.core.testing.TestRunner;
import io.resttestgen.core.testing.TestSequence;
import io.resttestgen.core.testing.mutator.OperationMutator;
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
import java.util.ArrayList;
import java.util.List;
import java.util.stream.Collectors;

/**
 * Breaching fuzzer: if a mutated request is rejected (4xx), mutate THAT rejected
 * request and attack again. Keeps evolving the attack until the target is breached
 * (2xx on bad input = bug) or the attempt budget is exhausted.
 *
 * Scoring:
 *   2xx on bad input  → 100  (BREACH — API accepted invalid data)
 *   5xx               → 80   (CRASH — server error, critical bug)
 *   400 / 422         → 40   (validation reached — server understood, close to breach)
 *   404               → 20   (resource not found — medium distance)
 *   401 / 403         → 10   (auth wall — far from breach)
 *   other             → 5
 *
 * Hill-climbing: always mutate from the highest-scoring request seen so far.
 * If a new mutation scores higher than the current best, it becomes the new base.
 */
public class BreachingFuzzer extends Fuzzer {

    private static final Logger logger = LogManager.getLogger(BreachingFuzzer.class);

    private final TestSequence testSequenceToMutate;
    private final List<MutateRandomParameterWithParameterMutatorOperationMutator> mutators;

    public BreachingFuzzer(TestSequence testSequenceToMutate) {
        this.testSequenceToMutate = testSequenceToMutate;

        mutators = new ArrayList<>();
        mutators.add(new MutateRandomParameterWithParameterMutatorOperationMutator(new MissingRequiredParameterMutator()));
        mutators.add(new MutateRandomParameterWithParameterMutatorOperationMutator(new WrongTypeParameterMutator()));
        mutators.add(new MutateRandomParameterWithParameterMutatorOperationMutator(new ConstraintViolationParameterMutator()));
    }

    /**
     * @param budget total number of attack attempts per nominal interaction
     */
    public List<TestSequence> generateTestSequences(int budget) {
        List<TestSequence> results = new ArrayList<>();

        for (TestInteraction nominalInteraction : testSequenceToMutate) {
            results.addAll(runBreachingLoop(nominalInteraction.getFuzzedOperation(), budget));
        }

        return results;
    }

    private List<TestSequence> runBreachingLoop(Operation nominalOperation, int budget) {
        List<TestSequence> results = new ArrayList<>();

        // Start from the nominal operation
        // Use array wrappers so lambda can reference them
        Operation[] bestCandidate = { nominalOperation };
        int[] bestScore = { 0 };
        boolean[] breached = { false };

        logger.info("[BreachingFuzzer] Starting breach attempt on '{}' with budget={}",
                nominalOperation.getOperationId(), budget);

        for (int attempt = 0; attempt < budget; attempt++) {

            // Pick a random applicable mutator
            final Operation currentCandidate = bestCandidate[0];
            List<MutateRandomParameterWithParameterMutatorOperationMutator> applicable =
                    mutators.stream()
                            .filter(m -> m.isOperationMutable(currentCandidate))
                            .collect(Collectors.toList());

            if (applicable.isEmpty()) {
                logger.warn("[BreachingFuzzer] No applicable mutators for '{}'. Stopping.",
                        nominalOperation.getOperationId());
                break;
            }

            OperationMutator mutator = Environment.getInstance().getRandom()
                    .nextElement(applicable).get();

            // Mutate the CURRENT BEST CANDIDATE (not always the nominal)
            Operation mutated = mutator.mutate(currentCandidate);

            // Build and fire the request
            TestInteraction interaction = new TestInteraction(mutated);
            interaction.addTag("breaching-attempt-" + attempt);

            TestSequence seq = new TestSequence(this, interaction);
            String seqName = !nominalOperation.getOperationId().isEmpty()
                    ? nominalOperation.getOperationId()
                    : nominalOperation.getMethod() + "-" + nominalOperation.getEndpoint();
            seq.setName(seqName);
            seq.appendGeneratedAtTimestampToSequenceName();

            TestRunner.getInstance().run(seq);

            // Score the response
            int score = score(seq.getLast());
            int statusCode = seq.getLast().getResponseStatusCode().getCode();

            logger.info("[BreachingFuzzer] attempt={} mutator={} status={} score={} bestScore={}",
                    attempt,
                    getMutatorName(mutator),
                    statusCode,
                    score,
                    bestScore[0]);

            // Evaluate with oracle
            ErrorStatusCodeOracle oracle = new ErrorStatusCodeOracle();
            oracle.assertTestSequence(seq);

            // Write report
            try {
                new ReportWriter(seq).write();
                new RestAssuredWriter(seq).write();
            } catch (IOException e) {
                logger.warn("[BreachingFuzzer] Could not write report.");
            }

            results.add(seq);

            // Hill-climb: if this mutation scored higher, it becomes the new base
            if (score > bestScore[0]) {
                bestScore[0] = score;
                bestCandidate[0] = mutated;
                logger.info("[BreachingFuzzer] New best candidate! score={} status={} — mutating THIS next.",
                        score, statusCode);
            }

            // Breach detected
            if (seq.getLast().getResponseStatusCode().isSuccessful()) {
                breached[0] = true;
                logger.warn("[BreachingFuzzer] *** BREACH on '{}' at attempt {} *** status={}",
                        nominalOperation.getOperationId(), attempt, statusCode);
                logger.warn("[BreachingFuzzer] Request URL: {}",
                        seq.getLast().getRequestURL());
                // Keep attacking from this weak point — don't stop
            }

            // Crash detected
            if (seq.getLast().getResponseStatusCode().isServerError()) {
                logger.warn("[BreachingFuzzer] *** CRASH on '{}' at attempt {} *** status={}",
                        nominalOperation.getOperationId(), attempt, statusCode);
            }
        }

        logger.info("[BreachingFuzzer] Finished '{}': breached={} bestScore={} attempts={}",
                nominalOperation.getOperationId(), breached[0], bestScore[0], budget);

        return results;
    }

    /**
     * Scores a response by how close it is to a breach.
     * Higher = closer to breaching the target.
     */
    private int score(TestInteraction interaction) {
        int code = interaction.getResponseStatusCode().getCode();

        if (code >= 200 && code < 300) return 100;  // BREACH — bad input accepted
        if (code >= 500)               return 80;   // CRASH — server error
        if (code == 400 || code == 422) return 40;  // Validation hit — server understood it, close
        if (code == 404)               return 20;   // Not found — medium distance
        if (code == 401 || code == 403) return 10;  // Auth wall — far from breach
        return 5;
    }

    private String getMutatorName(OperationMutator mutator) {
        if (mutator instanceof MutateRandomParameterWithParameterMutatorOperationMutator) {
            return ((MutateRandomParameterWithParameterMutatorOperationMutator) mutator)
                    .getParameterMutator().getClass().getSimpleName();
        }
        return mutator.getClass().getSimpleName();
    }
}
