# Phase 0 — coverage and feasibility audit

Read-only snapshot. A `no` in **L3 feasible** disables internal synthesis for that
indication only — drug facts, published evidence, question generation and
structured evidence gaps all still ship.

**Focus connected** asks whether Rinvoq/Skyrizi/Tremfya/Humira sit in ONE component,
not whether the whole swept graph is connected. Unrelated agents pulled in by the
search form their own islands and say nothing about the comparisons in scope.

**Catalog %** is the share of nodes resolving to a curated drug. Where it is low the
node count is inflated by uncurated labels — treat topology figures as an upper bound.

| Indication | RCTs | Screened out | With results | Arm-level | Nodes | Catalog % | Focus connected | Indep. loops | NMAs | L3 feasible |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | :---: | ---: | ---: | :---: |
| Plaque Psoriasis | 55 | 17 | 36 | 35 | 40 | 20% | yes | 7 | 26 | yes |
| Rheumatoid Arthritis | 39 | 11 | 21 | 21 | 44 | 14% | yes | 0 | 27 | yes |
| Psoriatic Arthritis | 37 | 10 | 19 | 19 | 54 | 19% | yes | 1 | 28 | yes |
| Atopic Dermatitis | 10 | 2 | 7 | 7 | 7 | 43% | no | 0 | 26 | NO |
| Ulcerative Colitis | 29 | 11 | 13 | 13 | 53 | 13% | yes | 1 | 22 | yes |
| Crohn's Disease | 44 | 23 | 13 | 13 | 65 | 12% | yes | 1 | 25 | yes |
| Ankylosing Spondylitis | 19 | 5 | 11 | 11 | 19 | 26% | yes | 0 | 21 | yes |
| Non-radiographic Axial Spondyloarthritis | 3 | 0 | 2 | 2 | 4 | 50% | no | 0 | 2 | NO |

## Plaque Psoriasis

- **Canonical outcomes**: PSO_PASI75_W16, PSO_PASI90_W16, PSO_PASI100_W16
- **Focus drugs in network**: Skyrizi, Tremfya, Humira
- **Induction/maintenance**: INDUCTION 1, MAINTENANCE 2, PRIMARY 52. Separable: yes
- **Placebo response by route**: not measurable — only one route observed for PSO_PASI100_W16, PSO_PASI75_W16, PSO_PASI90_W16
- **Pair connectivity**:
  - Rinvoq vs Humira: absent from network
  - Rinvoq vs Skyrizi: absent from network
  - Rinvoq vs Tremfya: absent from network
  - Skyrizi vs Humira: DIRECT
  - Skyrizi vs Tremfya: via Cosentyx, Humira, Placebo, Stelara
  - Tremfya vs Humira: DIRECT
- **Candidate published NMAs** (Level-2 input):
  - Abdullah A et al. (2026). Comparative Effectiveness of Four Biologics for Moderate-to-Severe Plaque Psoriasis: A Network Meta-Analysis. Cureus. PMID:42181314
  - Ye Y et al. (2026). Efficacy and safety of ustekinumab biosimilars for treating moderate-to-severe plaque psoriasis: a systematic review and network meta-analysis. Naunyn-Schmiedeberg's archives of pharmacology. PMID:41957184
  - Yu T et al. (2026). NMAstudio 2.0: An interactive tool for network meta-analysis to enhance understanding, interpretation, and communication of the findings. Research synthesis methods. PMID:41789459
  - Chen D et al. (2025). Effectiveness and safety of dietary supplements in the adjunctive treatment of psoriasis: a systematic review and network meta-analysis. Frontiers in nutrition. PMID:41459063
  - Bright HRB et al. (2026). Serious Infection Risk with Systemic Treatments for Psoriasis: A Systematic Review and Network Meta-analysis Combining Randomised and Non-randomised Evidence. Dermatology and therapy. PMID:41171589
  - Asahina A et al. (2026). Efficacy of Biologics for the Treatment of Moderate-To-Severe Plaque Psoriasis in the Asian Population: A Systematic Review and Network Meta-Analysis. International journal of dermatology. PMID:41115146
  - Aljalfan AA et al. (2026). Biologics for treatment of paediatric plaque psoriasis: A systematic review and network meta-analysis. Journal of the European Academy of Dermatology and Venereology : JEADV. PMID:41090545
  - Lebwohl MG et al. (2025). Correction: Biologics for the Treatment of Moderate-to-Severe Plaque Psoriasis: A Systematic Review and Network Meta-analysis. Dermatology and therapy. PMID:40855032
  - Chen J et al. (2025). A systematic review and network meta-analysis comparing the efficacy and safety of deucravacitinib versus selected treatments for moderate to severe plaque psoriasis. Clinical rheumatology. PMID:40846809
  - Sbidian E et al. (2025). Systemic pharmacological treatments for chronic plaque psoriasis: a network meta-analysis. The Cochrane database of systematic reviews. PMID:40767824
- **Source licensing**: ClinicalTrials.gov PUBLIC_DOMAIN, PubMed metadata PUBLIC_DOMAIN, full text per-article (see licence_for_pmc_record)

## Rheumatoid Arthritis

- **Canonical outcomes**: RA_ACR20_W12, RA_ACR50_W12, RA_ACR70_W12
- **Focus drugs in network**: Rinvoq, Humira
- **Induction/maintenance**: MAINTENANCE 1, PRIMARY 38 (1 trial(s) name both phases and were left PRIMARY — their substudies must be split before either can enter a network). Separable: no
- **Placebo response by route**: not measurable — only one route observed for RA_ACR20_W12, RA_ACR50_W12
- **Pair connectivity**:
  - Rinvoq vs Humira: via Placebo
  - Rinvoq vs Skyrizi: absent from network
  - Rinvoq vs Tremfya: absent from network
  - Skyrizi vs Humira: absent from network
  - Skyrizi vs Tremfya: absent from network
  - Tremfya vs Humira: absent from network
- **Candidate published NMAs** (Level-2 input):
  - Thomas J et al. (2026). Disease-modifying antirheumatic drugs (DMARDs) for rheumatoid arthritis after failure of biologic or targeted synthetic therapy: a systematic review and network meta-analysis. The Cochrane database of systematic reviews. PMID:42440279
  - Gibson M et al. (2026). Cancer risk of Janus kinase inhibitors and other advanced therapies in immune-mediated inflammatory diseases: a systematic review and Bayesian network meta-analysis of RCTs. Annals of the rheumatic diseases. PMID:42431784
  - Zhang XM et al. (2026). Infection risks associated with b/tsDMARDs in rheumatoid arthritis: a systematic review and network meta-analysis. European journal of clinical pharmacology. PMID:42426413
  - Li G et al. (2026). Network meta-analysis of novel diagnostic biomarkers for rheumatoid arthritis: comparative performance of anti-CarP, anti-MCV, and emerging markers. Frontiers in immunology. PMID:42382770
  - van Esveld L et al. (2026). Effects of biologic and targeted synthetic disease modifying antirheumatic drugs (b/tsDMARDs) on patient-reported outcome domains in rheumatoid arthritis: a systematic review and network meta-analyses. RMD open. PMID:42342282
  - Hu Y et al. (2026). Efficacy, safety, and tolerability of treatments for interstitial lung disease associated with rtoid arthritis: A systematic review and network meta-analysis. Journal of autoimmunity. PMID:42322667
  - Sehgal A et al. (2026). Prescription pattern assessment of pharmacotherapeutics used in the management of amyloidosis secondary to rheumatoid arthritis via systematic review and network meta-analysis. Journal of family medicine and primary care. PMID:42023360
  - Guski LS et al. (2026). Effect of monotherapy with conventional synthetic disease-modifying anti-rheumatic drugs or glucocorticoids on radiographic progression in rheumatoid arthritis: a network meta-analysis of 64 treatment arms from 31 randomized controlled trials. Scandinavian journal of rheumatology. PMID:41858241
  - Kamso MM et al. (2026). A semi-automated approach facilitated the assessment of the certainty of evidence in a network meta-analysis: Part 1 - Direct comparisons. Journal of clinical epidemiology. PMID:41421719
  - Kamso MM et al. (2026). A semi-automated approach facilitated the assessment of the certainty of evidence in a network meta-analysis: Part 2 - Indirect and Mixed comparisons. Journal of clinical epidemiology. PMID:41412485
- **Source licensing**: ClinicalTrials.gov PUBLIC_DOMAIN, PubMed metadata PUBLIC_DOMAIN, full text per-article (see licence_for_pmc_record)

## Psoriatic Arthritis

- **Canonical outcomes**: PSA_ACR20_W16, PSA_ACR50_W16, PSA_PASI90_W16
- **Focus drugs in network**: Rinvoq, Skyrizi, Tremfya, Humira
- **Induction/maintenance**: PRIMARY 37. Separable: no
- **Placebo response by route**: not measurable — only one route observed for PSA_ACR20_W16, PSA_ACR50_W16, PSA_PASI90_W16
- **Pair connectivity**:
  - Rinvoq vs Humira: DIRECT
  - Rinvoq vs Skyrizi: via Humira, Placebo
  - Rinvoq vs Tremfya: via Humira, Placebo
  - Skyrizi vs Humira: DIRECT
  - Skyrizi vs Tremfya: via Humira, Placebo
  - Tremfya vs Humira: DIRECT
- **Candidate published NMAs** (Level-2 input):
  - Gibson M et al. (2026). Cancer risk of Janus kinase inhibitors and other advanced therapies in immune-mediated inflammatory diseases: a systematic review and Bayesian network meta-analysis of RCTs. Annals of the rheumatic diseases. PMID:42431784
  - Annfeldt TK et al. (2026). Composite outcome measures that successfully differentiate active treatments from placebo in psoriatic arthritis trials: a GRAPPA-OMERACT systematic review and network meta-analysis. Annals of the rheumatic diseases. PMID:42248769
  - Gao S et al. (2025). Efficacy and safety of IL-17, IL-12/23, and IL-23 inhibitors for psoriatic arthritis: a network meta-analysis of randomized controlled trials. Frontiers in immunology. PMID:41050680
  - Sbidian E et al. (2025). Systemic pharmacological treatments for chronic plaque psoriasis: a network meta-analysis. The Cochrane database of systematic reviews. PMID:40767824
  - Shi LH et al. (2025). Risk of Major Adverse Cardiovascular Events and Thromboembolism Events in Patients with Psoriatic Arthritis on JAK Inhibitors: A Network Meta-Analysis. Rheumatology and therapy. PMID:40684063
  - Tsiogkas SG et al. (2025). Janus kinase inhibitors for psoriatic arthritis: Evidence from a systematic review and network meta-analysis. Autoimmunity reviews. PMID:40268128
  - Wan H et al. (2025). Comparative Efficacy and Safety of Different Regimens of Current JAK Inhibitors in Psoriatic Arthritis: A Network Meta-analysis. Journal of clinical rheumatology : practical reports on rheumatic & musculoskeletal diseases. PMID:40184480
  - Ink B et al. (2024). Comment on: Comparative short-term risks of infection and serious infection in patients receiving biologic and small-molecule therapies for psoriasis and psoriatic arthritis: a systemic review and network meta-analysis of randomized controlled trials. Therapeutic advances in chronic disease. PMID:40084160
  - Xie O et al. (2025). Biologics in the treatment of active Psoriatic arthritis in China: a network meta-analysis and cost-effectiveness analysis. Expert review of pharmacoeconomics & outcomes research. PMID:39783044
  - Lin J et al. (2024). Different biologics for biological-naïve patients with psoriatic arthritis: a systematic review and network meta-analysis. Frontiers in pharmacology. PMID:38545545
- **Source licensing**: ClinicalTrials.gov PUBLIC_DOMAIN, PubMed metadata PUBLIC_DOMAIN, full text per-article (see licence_for_pmc_record)

## Atopic Dermatitis

- **Canonical outcomes**: AD_EASI75_W16, AD_EASI90_W16, AD_IGA01_W16
- **Focus drugs in network**: Rinvoq
- **Induction/maintenance**: PRIMARY 10. Separable: no
- **Placebo response by route**: not measurable — only one route observed for AD_EASI75_W16, AD_EASI90_W16
- **Pair connectivity**:
  - Rinvoq vs Humira: absent from network
  - Rinvoq vs Skyrizi: absent from network
  - Rinvoq vs Tremfya: absent from network
  - Skyrizi vs Humira: absent from network
  - Skyrizi vs Tremfya: absent from network
  - Tremfya vs Humira: absent from network
- **Candidate published NMAs** (Level-2 input):
  - Ling S et al. (2026). Stringent Outcomes for Targeted Systemic Monotherapies in Moderate-To-Severe Atopic Dermatitis: A Network Meta-Analysis. Clinical and experimental allergy : journal of the British Society for Allergy and Clinical Immunology. PMID:42487227
  - Yang L et al. (2026). Comparative efficacy of pediatric atopic dermatitis treatments: a network meta-analysis highlighting dupilumab and pimecrolimus for SCORAD and EASI improvement. Frontiers in immunology. PMID:42148141
  - Xiong M et al. (2026). Comparative efficacy of targeted systemic therapies for moderate-to-severe atopic dermatitis: a network meta-analysis of phase 3-4 randomized trials. The Journal of dermatological treatment. PMID:41919337
  - Sarsik S et al. (2026). Comparative Efficacy and Safety of Tapinarof 0.5% and 1% Cream Regimens for Atopic Dermatitis: A Network Meta-Analysis. Dermatology practical & conceptual. PMID:41912176
  - Babul A et al. (2026). Biologic Monotherapies for Moderate-to-Severe Atopic Dermatitis: A Systematic Review and Bayesian Network Meta-Analysis of Established and Investigational Agents. Cureus. PMID:41884322
  - Zeng L et al. (2026). Risk assessment of asthma and allergic rhinitis in atopic dermatitis patients treated with biologics and JAK inhibitors: a systematic review and network meta-analysis of randomized controlled trials. BMC medicine. PMID:41652607
  - Li KH et al. (2026). Biologic Treatments in Adolescents With Moderate-to-Severe Atopic Dermatitis: A Systematic Literature Review and Network Meta-analysis. The Annals of pharmacotherapy. PMID:41635230
  - Pink AE et al. (2026). Comparing the Efficacy and Safety of Nemolizumab Versus Anti-interleukin Monoclonal Antibody Therapies in Combination with Topical Treatments for Moderate-to-Severe Atopic Dermatitis Using Network Meta-analysis. Dermatology and therapy. PMID:41553701
  - Babul A et al. (2026). Upadacitinib Leads in Efficacy: A Bayesian Network Meta-Analysis of Four JAK Inhibitors in Moderate-To-Severe Atopic Dermatitis. International journal of dermatology. PMID:41489415
  - Cai W et al. (2025). Efficacy and Safety of All Monoclonal Antibodies in Moderate-to-Severe Atopic Dermatitis: A Systematic Review and Network Meta-Analysis. Pharmacoepidemiology and drug safety. PMID:41346302
- **Source licensing**: ClinicalTrials.gov PUBLIC_DOMAIN, PubMed metadata PUBLIC_DOMAIN, full text per-article (see licence_for_pmc_record)

## Ulcerative Colitis

- **Canonical outcomes**: UC_REMISSION_INDUCTION_W8, UC_ENDOSCOPIC_IMPROVEMENT_INDUCTION_W8, UC_REMISSION_MAINTENANCE_W52
- **Focus drugs in network**: Rinvoq, Skyrizi, Tremfya, Humira
- **Induction/maintenance**: INDUCTION 6, MAINTENANCE 1, PRIMARY 22 (7 trial(s) name both phases and were left PRIMARY — their substudies must be split before either can enter a network). Separable: yes
- **Placebo response by route** (on UC_REMISSION_MAINTENANCE_W52): ORAL 18.8% (n=3), SC 5.1% (n=5) — spread 13.7pp. material difference; SENSITIVITY_REQUIRED or SUBGROUP_BY_ROUTE
- **Pair connectivity**:
  - Rinvoq vs Humira: via Placebo
  - Rinvoq vs Skyrizi: via Placebo
  - Rinvoq vs Tremfya: via Placebo
  - Skyrizi vs Humira: via Placebo
  - Skyrizi vs Tremfya: via Placebo
  - Tremfya vs Humira: via Placebo
- **Candidate published NMAs** (Level-2 input):
  - Giri S et al. (2026). Comparative efficacy of advanced therapies in biological-exposed ulcerative colitis - a network meta-analysis of randomized trials. European journal of gastroenterology & hepatology. PMID:42429193
  - Li X et al. (2026). Systematic review and network meta-analysis of integrated traditional Chinese and conventional medicine for ulcerative colitis. Frontiers in pharmacology. PMID:42428503
  - Xu R et al. (2026). Efficacy and safety of dietary supplements for the treatment of ulcerative colitis, a network meta-analysis. Frontiers in medicine. PMID:42100281
  - Wu H et al. (2025). Safety and efficacy of different JAK inhibitors in the treatment of inflammatory bowel disease: a network meta-analysis. Frontiers in pharmacology. PMID:41675268
  - Sawaf B et al. (2025). Interleukin 12/23 and interleukin 23 inhibitors for moderate-to-severe ulcerative colitis: a systematic review and network meta-analysis. Annals of gastroenterology. PMID:41586395
  - Zhang L et al. (2025). Comparative clinical efficacy of acupuncture-related therapies for ulcerative colitis: a systematic review and network meta-analysis. Frontiers in medicine. PMID:41458494
  - Xu Y et al. (2025). Second-line treatment strategies of ulcerative colitis after conventional therapy failure: A systematic review and network meta-analysis of randomized controlled trials. PloS one. PMID:41325370
  - Katsoula A et al. (2025). Systematic review and network meta-analysis: evaluating the impact of advanced therapies for moderate-to-severe ulcerative colitis on health-related quality of life. Journal of Crohn's & colitis. PMID:41284688
  - Hayek MA et al. (2025). Comparative efficacy of immunomodulators, biologics, and advanced therapies for steroid-refractory acute severe ulcerative colitis: A network meta-analysis and time-to-event analysis. Digestive and liver disease : official journal of the Italian Society of Gastroenterology and the Italian Association for the Study of the Liver. PMID:41188167
  - Wei K et al. (2025). The efficacy of dietary therapies in modulating inflammatory biomarkers, clinical remission and quality of life in patients with inflammatory bowel disease: a network meta-analysis of 15 interventions. Frontiers in nutrition. PMID:41122500
- **Source licensing**: ClinicalTrials.gov PUBLIC_DOMAIN, PubMed metadata PUBLIC_DOMAIN, full text per-article (see licence_for_pmc_record)

## Crohn's Disease

- **Canonical outcomes**: CD_REMISSION_INDUCTION_W12, CD_ENDOSCOPIC_RESPONSE_INDUCTION_W12, CD_REMISSION_MAINTENANCE_W52
- **Focus drugs in network**: Rinvoq, Skyrizi, Tremfya, Humira
- **Induction/maintenance**: INDUCTION 9, MAINTENANCE 2, PRIMARY 33 (8 trial(s) name both phases and were left PRIMARY — their substudies must be split before either can enter a network). Separable: yes
- **Placebo response by route** (on CD_ENDOSCOPIC_RESPONSE_INDUCTION_W12): ORAL 3.5% (n=1), SC 12.8% (n=5) — spread 9.3pp. material difference; SENSITIVITY_REQUIRED or SUBGROUP_BY_ROUTE
- **Pair connectivity**:
  - Rinvoq vs Humira: via Entyvio, Placebo
  - Rinvoq vs Skyrizi: via Placebo
  - Rinvoq vs Tremfya: via Placebo
  - Skyrizi vs Humira: via Placebo
  - Skyrizi vs Tremfya: DIRECT
  - Tremfya vs Humira: via Placebo
- **Candidate published NMAs** (Level-2 input):
  - Gordon M et al. (2026). Biologic drugs for induction and maintenance of remission in Crohn's disease: a network meta-analysis. The Cochrane database of systematic reviews. PMID:42333672
  - Chen W et al. (2026). Pharmacological Strategies for Preventing Postoperative Recurrence in Crohn's Disease: A Systematic Review and Network Meta-Analysis of Randomized Controlled Trials. Medicina (Kaunas, Lithuania). PMID:42195136
  - Chen J et al. (2026). Enteral nutrition versus immunomodulators for induction and maintenance of remission in pediatric Crohn's disease: a systematic review and network meta-analysis. Frontiers in pediatrics. PMID:42099516
  - Qtaishat FA et al. (2026). JAK Inhibitors for Crohn's Disease: A Systematic Review and Dose-Response Network Meta-Analysis of Efficacy and Safety. JGH open : an open access journal of gastroenterology and hepatology. PMID:42022940
  - Wu H et al. (2025). Safety and efficacy of different JAK inhibitors in the treatment of inflammatory bowel disease: a network meta-analysis. Frontiers in pharmacology. PMID:41675268
  - Schreiber S et al. (2026). Comparative Efficacy and Safety of Advanced Therapies in Maintenance Treatment of Adult Patients with Moderate-to-Severe Crohn's Disease: A Systematic Literature Review and Network Meta-Analysis. Advances in therapy. PMID:41553714
  - Khoshaim YA et al. (2025). Filgotinib in Moderate-to-Severe Crohn's Disease: A Network Meta-Analysis of Efficacy and Adverse Events. Healthcare (Basel, Switzerland). PMID:41516937
  - Gordon M et al. (2025). Interventions for maintenance of surgically induced remission in Crohn's disease: a systematic review and network meta-analysis. BMJ open gastroenterology. PMID:41423256
  - Kaneko M et al. (2025). Efficacy and safety of IL-23p19 and IL-12/23p40 inhibitors in moderate-to-severe Crohn's disease: a systematic review and network meta-analysis. Annals of medicine. PMID:41339249
  - Versteegh M et al. (2026). Comparative Efficacy of all Available Pharmaceutical Therapies for Moderate to Severe Crohn's Disease: A Systematic Review and Network Meta-Analysis. Gastro hep advances. PMID:41140764
- **Source licensing**: ClinicalTrials.gov PUBLIC_DOMAIN, PubMed metadata PUBLIC_DOMAIN, full text per-article (see licence_for_pmc_record)

## Ankylosing Spondylitis

- **Canonical outcomes**: AS_ASAS40_W14
- **Focus drugs in network**: Rinvoq, Skyrizi, Humira
- **Induction/maintenance**: PRIMARY 19. Separable: no
- **Placebo response by route**: not measurable — only one route observed for AS_ASAS40_W14
- **Pair connectivity**:
  - Rinvoq vs Humira: via Placebo
  - Rinvoq vs Skyrizi: via Placebo
  - Rinvoq vs Tremfya: absent from network
  - Skyrizi vs Humira: via Placebo
  - Skyrizi vs Tremfya: absent from network
  - Tremfya vs Humira: absent from network
- **Candidate published NMAs** (Level-2 input):
  - Kong L et al. (2025). Comparative efficacy of different exercise interventions in patients with ankylosing spondylitis: a systematic review and network meta-analysis. PeerJ. PMID:41321953
  - Han K et al. (2025). Different Acupuncture Therapies Combined with Sulfasalazine for the Treatment of Ankylosing Spondylitis: Bayesian Network Meta-Analysis. Journal of pain research. PMID:41235077
  - Zhao X et al. (2025). Risk of new-onset and recurrent uveitis with different biologics for ankylosing spondylitis: a network meta-analysis. Frontiers in immunology. PMID:40621455
  - Shi J et al. (2025). Posterior treatment of ankylosing spinal diseases with thoracolumbar fractures: a network meta-analysis. BMC musculoskeletal disorders. PMID:40312373
  - Liu X et al. (2025). Efficacy and Safety of Interleukin-17 and Janus Kinase Inhibitors in Ankylosing Spondylitis: A Systematic Review and Network Meta-Analysis. International archives of allergy and immunology. PMID:40010328
  - Shi J et al. (2024). Targeted therapies and conventional care for the treatment of ankylosing spondylitis in China: a cost-effectiveness analysis based on the network-meta analysis. Journal of orthopaedic surgery and research. PMID:39155381
  - Luo Y et al. (2024). Effectiveness of exercise intervention in relieving symptoms of ankylosing spondylitis: A network meta-analysis. PloS one. PMID:38875227
  - Zhou E et al. (2023). Comparison of biologics and small-molecule drugs in axial spondyloarthritis: a systematic review and network meta-analysis. Frontiers in pharmacology. PMID:37942485
  - Mattay SS et al. (2024). Risk of Major Adverse Cardiovascular Events in Immune-Mediated Inflammatory Disorders on Biologics and Small Molecules: Network Meta-Analysis. Clinical gastroenterology and hepatology : the official clinical practice journal of the American Gastroenterological Association. PMID:37821035
  - Tian C et al. (2023). Efficacy and safety of IL inhibitors, TNF-α inhibitors, and JAK inhibitors in patients with ankylosing spondylitis: a systematic review and Bayesian network meta-analysis. Annals of translational medicine. PMID:36923085
- **Source licensing**: ClinicalTrials.gov PUBLIC_DOMAIN, PubMed metadata PUBLIC_DOMAIN, full text per-article (see licence_for_pmc_record)

## Non-radiographic Axial Spondyloarthritis

- **Canonical outcomes**: NRAXSPA_ASAS40_W14
- **Focus drugs in network**: Humira
- **Induction/maintenance**: PRIMARY 3. Separable: no
- **Placebo response by route**: not measurable — only one route observed for NRAXSPA_ASAS40_W14
- **Pair connectivity**:
  - Rinvoq vs Humira: absent from network
  - Rinvoq vs Skyrizi: absent from network
  - Rinvoq vs Tremfya: absent from network
  - Skyrizi vs Humira: absent from network
  - Skyrizi vs Tremfya: absent from network
  - Tremfya vs Humira: absent from network
- **Candidate published NMAs** (Level-2 input):
  - Li D et al. (2025). Efficacy and Safety of Tumor Necrosis Factor Inhibitors, Interleukin-17 Inhibitors, and Janus Kinase Inhibitors in Patients with Non-Radiographic Axial Spondyloarthritis: A Systematic Review and Network Meta-Analysis. International archives of allergy and immunology. PMID:39657601
  - Zhou E et al. (2023). Comparison of biologics and small-molecule drugs in axial spondyloarthritis: a systematic review and network meta-analysis. Frontiers in pharmacology. PMID:37942485
- **Source licensing**: ClinicalTrials.gov PUBLIC_DOMAIN, PubMed metadata PUBLIC_DOMAIN, full text per-article (see licence_for_pmc_record)
