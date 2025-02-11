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

package com.logicalclocks.hsfs.constructor;

import com.logicalclocks.hsfs.metadata.FeatureGroupBase;
import lombok.AllArgsConstructor;
import lombok.Getter;
import lombok.NoArgsConstructor;
import lombok.Setter;

@AllArgsConstructor
@NoArgsConstructor
public class HudiFeatureGroupAlias {

  @Getter
  @Setter
  private String alias;

  @Getter
  @Setter
  private FeatureGroupBase featureGroup;

  @Getter
  @Setter
  private Long leftFeatureGroupStartTimestamp;

  @Getter
  @Setter
  private Long leftFeatureGroupEndTimestamp;
}
