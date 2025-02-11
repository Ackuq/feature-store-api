/*
 * Copyright (c) 2020 Logical Clocks AB
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 * http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 *
 * See the License for the specific language governing permissions and limitations under the License.
 */

package com.logicalclocks.hsfs.engine;

import com.amazon.deequ.checks.Check;
import com.amazon.deequ.checks.CheckResult;
import com.amazon.deequ.constraints.ConstraintResult;
import com.logicalclocks.hsfs.EntityEndpointType;
import com.logicalclocks.hsfs.FeatureStoreException;
import com.logicalclocks.hsfs.metadata.Expectation;
import com.logicalclocks.hsfs.metadata.ExpectationsApi;
import com.logicalclocks.hsfs.metadata.ExpectationResult;
import com.logicalclocks.hsfs.metadata.FeatureGroupBase;
import com.logicalclocks.hsfs.metadata.FeatureGroupValidation;
import com.logicalclocks.hsfs.metadata.FeatureGroupValidationsApi;
import com.logicalclocks.hsfs.metadata.ValidationResult;
import com.logicalclocks.hsfs.metadata.validation.Level;
import com.logicalclocks.hsfs.metadata.validation.Rule;
import com.logicalclocks.hsfs.metadata.validation.RuleName;
import org.apache.commons.lang3.tuple.ImmutablePair;
import org.apache.spark.sql.Dataset;
import org.apache.spark.sql.Row;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import scala.Option;
import scala.collection.JavaConverters;

import java.io.IOException;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import com.google.common.base.Strings;

public class DataValidationEngine {

  private static DataValidationEngine INSTANCE = null;

  public static synchronized DataValidationEngine getInstance() {
    if (INSTANCE == null) {
      INSTANCE = new DataValidationEngine();
    }
    return INSTANCE;
  }

  private final FeatureGroupValidationsApi featureGroupValidationsApi =
      new FeatureGroupValidationsApi(EntityEndpointType.FEATURE_GROUP);

  private final ExpectationsApi expectationsApi = new ExpectationsApi(EntityEndpointType.FEATURE_GROUP);

  private static final Logger LOGGER = LoggerFactory.getLogger(DataValidationEngine.class);

  public <S> FeatureGroupValidation validate(S data, FeatureGroupBase featureGroupBase,
                                         List<Expectation> expectations,
                                         Boolean logActivity)
      throws FeatureStoreException, IOException {
    List<ExpectationResult> expectationResults = validate(data, expectations);
    return featureGroupValidationsApi.put(featureGroupBase,
        FeatureGroupValidation.builder()
            .validationTime(Instant.now().toEpochMilli())
            .expectationResults(expectationResults).build(),
        logActivity);
  }

  private <S> List<ExpectationResult> validate(S data, List<Expectation> expectations) {
    // Loop through all feature group expectations, then loop all features and rules of the expectation and
    // create constraints for Deequ.
    List<ExpectationResult> expectationResults = new ArrayList<>();
    for (Expectation expectation : expectations) {
      List<ConstraintGroup> constraintGroups = new ArrayList<>();
      Map<Level, List<Constraint>> constraintGroupLevels = new HashMap<>();
      List<ValidationResult> validationResults = new ArrayList<>();
      // An expectation contains all the features its rules are applied to but not every rule is applied to all features
      // Certain Compliance rules are applied on pairs of features which means the rule will be applied on all possible
      // combinations between the expectations features and the rule's "feature" (rule.getFeature().
      for (Rule rule : expectation.getRules()) {
        boolean comparativeRule = isRuleAppliedToFeaturePairs(rule);
        for (String feature : expectation.getFeatures()) {
          String[] legalValues = null;
          if (rule.getLegalValues() != null && !rule.getLegalValues().isEmpty()) {
            legalValues = rule.getLegalValues().toArray(new String[0]);
          }

          List<String> featuresToEval = comparativeRule && !feature.equals(rule.getFeature())
                                        ? Arrays.asList(feature, rule.getFeature())
                                        : Collections.singletonList(feature);

          Constraint constraint =
              new Constraint(rule.getName().name(),
              Option.apply(rule.getName().name()),
              Option
                .apply(JavaConverters.asScalaBufferConverter(featuresToEval).asScala().toSeq()),
              Option.apply(rule.getMin()),
              Option.apply(rule.getMax()),
              Option.apply(rule.getValue()),
              Option.apply(rule.getPattern()),
              Option.apply(rule.getAcceptedType()),
              Option.apply(legalValues));
          if (!constraintGroupLevels.containsKey(rule.getLevel())) {
            constraintGroupLevels.put(rule.getLevel(), new ArrayList<>());
          }
          constraintGroupLevels.get(rule.getLevel()).add(constraint);
        }
      }
      if (!constraintGroupLevels.isEmpty()) {
        for (Level level : constraintGroupLevels.keySet()) {
          ConstraintGroup constraintGroup = new ConstraintGroup(level.name(), level.name(),
              JavaConverters.asScalaIteratorConverter(constraintGroupLevels.get(level).iterator()).asScala().toSeq());
          constraintGroups.add(constraintGroup);
        }
      }

      // Run Deequ verification suite and return results
      Map<Check, CheckResult> deequResults = DeequEngine.runVerification((Dataset<Row>) data,
          JavaConverters.asScalaIteratorConverter(constraintGroups.iterator()).asScala().toSeq());
      // Parse Deequ results and convert to Feature Group validation results. Unfortunately we don't have a way of
      // getting the features and the constraint type directly from the ConstraintResult object so we need to parse
      // the String representation of the object and for every constraint type the representation follows a different
      // format. For every constraint type there is an example in the comments to assist.
      for (Check check : deequResults.keySet()) {
        List<ConstraintResult> constraintResultsList =
            DeequEngine.getConstraintResults(deequResults.get(check).constraintResults());
        for (ConstraintResult constraintResult : constraintResultsList) {
          String[] constraintInfo = constraintResult.constraint().toString().split("\\W+");
          String constraintType = constraintInfo[1];
          List<String> deequFeatures = new ArrayList<>();
          String deequRule = null;
          boolean constraintTypeComplex = false;
          if (constraintType.equals("Compliance")) { //IS_LESS_THAN etc.
            // ComplianceConstraint(Compliance(year is less than salary,year < salary,None))
            constraintTypeComplex = true;
            if (constraintResult.constraint().toString().contains("contained in")) {
              // ComplianceConstraint(Compliance(car contained in car15,car20,`car` IS NULL OR `car` ...
              deequRule = "iscontainedin";
              deequFeatures.add(constraintInfo[2]);
            } else if (constraintResult.constraint().toString().contains("is positive")) {
              // ComplianceConstraint(Compliance(age is positive,COALESCE(car, 1.0) > 0,None))
              deequRule = "ispositive";
              deequFeatures.add(constraintInfo[2]);
            } else {
              deequFeatures.addAll(Arrays.asList(
                      Arrays.stream(constraintInfo, constraintInfo.length - 3, constraintInfo.length - 2 + 1)
                              .toArray(String[]::new)));
              Pattern pattern = Pattern.compile(deequFeatures.get(0) + "(.*?)" + deequFeatures.get(1), Pattern.DOTALL);
              Matcher matcher = pattern.matcher(constraintResult.constraint().toString());
              if (matcher.find()) {
                deequRule = matcher.group(1).replaceAll(" ", "");
              }
            }
          } else {
            deequRule = constraintInfo[1];
            if (deequRule.equalsIgnoreCase("MutualInformation") || constraintType.equals("Correlation")) {
              constraintTypeComplex = true;
              if (constraintType.equals("MutualInformation")) {
                // MutualInformationConstraint(MutualInformation(List(year, salary),None))
                deequFeatures.add(constraintInfo[3]);
                deequFeatures.add(constraintInfo[4]);
              } else {
                // "CorrelationConstraint(Correlation(year,salary,None))
                deequFeatures.add(constraintInfo[2]);
                deequFeatures.add(constraintInfo[3]);
              }
            } else {
              // MinimumConstraint(Minimum(commission,None))...
              deequFeatures.add(constraintInfo[2]);
            }
          }

          RuleName ruleName = getRuleNameFromDeequ(deequRule);
          // Find rule from list of rules that Deequ used for validation
          if (constraintTypeComplex) {
            for (Rule rule : expectation.getRules()) {
              if (rule.getName() == ruleName) {
                validationResults.add(ValidationResult.builder()
                    .status(ExpectationResult.Status.fromDeequStatus(constraintResult.status(), rule.getLevel()))
                    .features(deequFeatures)
                    .rule(rule)
                    .message(!constraintResult.message().isEmpty() ? constraintResult.message().get() : "Success")
                    .value(String.valueOf(constraintResult.metric().get().value().get()))
                    .build());
              }
            }
          } else {
            for (String feature : expectation.getFeatures()) {
              for (Rule rule : expectation.getRules()) {
                if (rule.getName() == ruleName && feature.equals(constraintInfo[2])) {
                  validationResults.add(ValidationResult.builder()
                      .status(ExpectationResult.Status.fromDeequStatus(constraintResult.status(), rule.getLevel()))
                      .features(Collections.singletonList(feature))
                      .rule(rule)
                      .message(!constraintResult.message().isEmpty() ? constraintResult.message().get() : "Success")
                      .value(String.valueOf(constraintResult.metric().get().value().get()))
                      .build());
                  break;
                }
              }
            }
          }
        }
      }
      expectationResults.add(ExpectationResult.builder().expectation(expectation).results(validationResults).build());
    }
    return expectationResults;
  }

  public List<FeatureGroupValidation> getValidations(FeatureGroupBase featureGroupBase)
      throws FeatureStoreException, IOException {
    return featureGroupValidationsApi.get(featureGroupBase);
  }

  public FeatureGroupValidation getValidation(FeatureGroupBase featureGroupBase, ImmutablePair<ValidationTimeType,
                                              Long> pair)
      throws FeatureStoreException, IOException {
    return featureGroupValidationsApi.get(featureGroupBase, pair);
  }

  public RuleName getRuleNameFromDeequ(String rule) {
    if (Strings.isNullOrEmpty(rule)) {
      throw new IllegalArgumentException("Rule name cannot be null or empty");
    }
    switch (rule.toLowerCase()) {
      case "maximum":
        return RuleName.HAS_MAX;
      case "minimum":
        return RuleName.HAS_MIN;
      case "mean":
        return RuleName.HAS_MEAN;
      case "size":
        return RuleName.HAS_SIZE;
      case "sum":
        return RuleName.HAS_SUM;
      case "completeness":
        return RuleName.HAS_COMPLETENESS;
      case "uniqueness":
        return RuleName.HAS_UNIQUENESS;
      case "distinctness":
        return RuleName.HAS_DISTINCTNESS;
      case "uniquevalueratio":
        return RuleName.HAS_UNIQUE_VALUE_RATIO;
      case "histogram":
        return RuleName.HAS_NUMBER_OF_DISTINCT_VALUES;
      case "entropy":
        return RuleName.HAS_ENTROPY;
      case "mutualinformation":
        return RuleName.HAS_MUTUAL_INFORMATION;
      case "approxquantile":
        return RuleName.HAS_APPROX_QUANTILE;
      case "standarddeviation":
        return RuleName.HAS_STANDARD_DEVIATION;
      case "approxcountdistinct":
        return RuleName.HAS_APPROX_COUNT_DISTINCT;
      case "correlation":
        return RuleName.HAS_CORRELATION;
      case "patternmatch":
        return RuleName.HAS_PATTERN;
      case "minlength":
        return RuleName.HAS_MIN_LENGTH;
      case "maxlength":
        return RuleName.HAS_MAX_LENGTH;
      case "datatype":
        return RuleName.HAS_DATATYPE;
      case "isnonnegative":
        return RuleName.IS_NON_NEGATIVE;
      case "ispositive":
        return RuleName.IS_POSITIVE;
      case "islessthan":
        return RuleName.IS_LESS_THAN;
      case "islessthanorequalto":
        return RuleName.IS_LESS_THAN_OR_EQUAL_TO;
      case "isgreaterthan":
        return RuleName.IS_GREATER_THAN;
      case "isgreaterthanorequalto":
        return RuleName.IS_GREATER_THAN_OR_EQUAL_TO;
      case "iscontainedin":
        return RuleName.IS_CONTAINED_IN;
      default:
        throw new UnsupportedOperationException("Deequ rule not supported: " + rule);
    }
  }

  public static boolean isRuleAppliedToFeaturePairs(Rule rule) {
    return isRuleAppliedToFeaturePairs(rule.getName());
  }

  public static boolean isRuleAppliedToFeaturePairs(RuleName ruleName) {
    return isRuleAppliedToFeaturePairs(ruleName.name());
  }

  public static boolean isRuleAppliedToFeaturePairs(String ruleName) {
    return ruleName.equals(RuleName.IS_GREATER_THAN_OR_EQUAL_TO.name())
      || ruleName.equals(RuleName.IS_GREATER_THAN.name())
      || ruleName.equals(RuleName.IS_LESS_THAN.name())
      || ruleName.equals(RuleName.IS_LESS_THAN_OR_EQUAL_TO.name())
      || ruleName.equals(RuleName.HAS_MUTUAL_INFORMATION.name())
      || ruleName.equals(RuleName.HAS_CORRELATION.name());
  }

  public enum ValidationTimeType {
    VALIDATION_TIME,
    COMMIT_TIME
  }
}
