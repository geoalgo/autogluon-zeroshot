import copy
import time
from typing import List

import numpy as np
import ray
from sklearn.model_selection import KFold

from .configuration_list_scorer import ConfigurationListScorer
from .simulation_context import ZeroshotSimulatorContext


@ray.remote
def score_config_ray(config_scorer, existing_configs, new_config) -> float:
    configs = existing_configs + [new_config]
    score = config_scorer.score(configs)
    return score


class ZeroshotConfigGenerator:
    def __init__(self, config_scorer, configs: List[str], backend='ray'):
        self.config_scorer = config_scorer
        self.all_configs = configs
        self.backend = backend

    def select_zeroshot_configs(self,
                                num_zeroshot: int,
                                zeroshot_configs: List[str] = None,
                                removal_stage=True,
                                removal_threshold=0,
                                config_scorer_test=None,
                                ) -> List[str]:
        if zeroshot_configs is None:
            zeroshot_configs = []
        else:
            zeroshot_configs = copy.deepcopy(zeroshot_configs)

        iteration = 0
        if self.backend == 'ray':
            if not ray.is_initialized():
                ray.init()
            config_scorer = ray.put(self.config_scorer)
            selector = self._select_ray
        else:
            config_scorer = self.config_scorer
            selector = self._select_sequential
        while len(zeroshot_configs) < num_zeroshot:
            iteration += 1
            # greedily search the config that would yield the lowest average rank if we were to evaluate it in combination
            # with previously chosen configs.

            valid_configs = [c for c in self.all_configs if c not in zeroshot_configs]
            if not valid_configs:
                break


            time_start = time.time()
            best_next_config, best_score = selector(valid_configs, zeroshot_configs, config_scorer)
            time_end = time.time()

            zeroshot_configs.append(best_next_config)
            msg = f'{iteration}\t: {round(best_score, 2)} | {round(time_end-time_start, 2)}s | {self.backend}'
            if config_scorer_test:
                score_test = config_scorer_test.score(zeroshot_configs)
                msg += f'\tTest: {round(score_test, 2)}'
            msg += f'\t{best_next_config}'
            print(msg)

        if removal_stage:
            zeroshot_configs = self.prune_zeroshot_configs(zeroshot_configs, removal_threshold=removal_threshold)
        print(f"selected {zeroshot_configs}")
        return zeroshot_configs

    @staticmethod
    def _select_sequential(configs: list, prior_configs: list, config_scorer):
        best_next_config = None
        # todo could use np.inf but would need unit-test (also to check that ray/sequential returns the same selection)
        best_score = 999999999
        for config in configs:
            config_selected = prior_configs + [config]
            config_score = config_scorer.score(config_selected)
            if config_score < best_score:
                best_score = config_score
                best_next_config = config
        return best_next_config, best_score

    @staticmethod
    def _select_ray(configs: list, prior_configs: list, config_scorer):
        # Create and execute all tasks in parallel
        results = []
        for i in range(len(configs)):
            results.append(score_config_ray.remote(
                config_scorer,
                prior_configs,
                configs[i],
            ))
        result = ray.get(results)
        result_idx_min = result.index(min(result))
        best_next_config = configs[result_idx_min]
        best_score = result[result_idx_min]
        return best_next_config, best_score

    def prune_zeroshot_configs(self, zeroshot_configs: List[str], removal_threshold=0) -> List[str]:
        zeroshot_configs = copy.deepcopy(zeroshot_configs)
        best_score = self.config_scorer.score(zeroshot_configs)
        finished_removal = False
        while not finished_removal:
            best_remove_config = None
            for config in zeroshot_configs:
                config_selected = [c for c in zeroshot_configs if c != config]
                config_score = self.config_scorer.score(config_selected)

                if best_remove_config is None:
                    if config_score <= (best_score + removal_threshold):
                        best_score = config_score
                        best_remove_config = config
                else:
                    if config_score <= best_score:
                        best_score = config_score
                        best_remove_config = config
            if best_remove_config is not None:
                print(f'REMOVING: {best_score} | {best_remove_config}')
                zeroshot_configs.remove(best_remove_config)
            else:
                finished_removal = True
        return zeroshot_configs


class ZeroshotConfigGeneratorCV:
    def __init__(self,
                 n_splits: int,
                 zeroshot_simulator_context: ZeroshotSimulatorContext,
                 config_scorer: ConfigurationListScorer,
                 configs: List[str] = None,
                 backend='ray'):
        """
        Runs zero-shot selection on `n_splits` ("train", "test") folds of datasets.
        For each split, zero-shot configurations are selected using the datasets belonging on the "train" split and the
        performance of the zero-shot configuration is evaluated using the datasets in the "test" split.
        :param n_splits: number of split to consider
        :param zeroshot_simulator_context:
        :param config_scorer:
        :param configs:
        :param backend:
        """
        assert n_splits >= 2
        self.n_splits = n_splits
        self.backend = backend
        self.config_scorer = config_scorer
        self.unique_datasets_fold = np.array(config_scorer.datasets)
        self.unique_datasets_map = zeroshot_simulator_context.dataset_name_to_tid_dict
        self.unique_datasets = set()
        self.dataset_parent_to_fold_map = dict()
        for d in self.unique_datasets_fold:
            dataset_parent = self.unique_datasets_map[d]
            self.unique_datasets.add(dataset_parent)
            if dataset_parent in self.dataset_parent_to_fold_map:
                self.dataset_parent_to_fold_map[dataset_parent].append(d)
            else:
                self.dataset_parent_to_fold_map[dataset_parent] = [d]
        for d in self.dataset_parent_to_fold_map:
            self.dataset_parent_to_fold_map[d] = sorted(self.dataset_parent_to_fold_map[d])
        self.unique_datasets = np.array((sorted(list(self.unique_datasets))))

        if configs is None:
            configs = zeroshot_simulator_context.get_configs()
        self.configs = configs

        self.kf = KFold(n_splits=self.n_splits, random_state=0, shuffle=True)

    def run(self):
        fold_results = []
        for i, (train_index, test_index) in enumerate(self.kf.split(self.unique_datasets)):
            print(f'Fitting Fold {i+1}...')
            X_train, X_test = list(self.unique_datasets[train_index]), list(self.unique_datasets[test_index])
            X_train_fold = []
            X_test_fold = []
            for d in X_train:
                X_train_fold += self.dataset_parent_to_fold_map[d]
            for d in X_test:
                X_test_fold += self.dataset_parent_to_fold_map[d]
            zeroshot_configs_fold, score_fold = self.run_fold(X_train_fold, X_test_fold)
            results_fold = {
                'fold': i+1,
                'X_train': X_train,
                'X_test': X_test,
                'X_train_fold': X_train_fold,
                'X_test_fold': X_test_fold,
                'score': score_fold,
                'selected_configs': zeroshot_configs_fold,
            }
            fold_results.append(results_fold)
        return fold_results

    def run_fold(self, X_train, X_test):
        config_scorer_train = self.config_scorer.subset(datasets=X_train)
        config_scorer_test = self.config_scorer.subset(datasets=X_test)

        zs_config_generator = ZeroshotConfigGenerator(config_scorer=config_scorer_train,
                                                      configs=self.configs,
                                                      backend=self.backend)

        zeroshot_configs = zs_config_generator.select_zeroshot_configs(10,
                                                                       removal_stage=False,
                                                                       # config_scorer_test=config_scorer_test
                                                                       )
        # deleting
        # FIXME: SPEEDUP WITH RAY
        # zeroshot_configs = zs_config_generator.prune_zeroshot_configs(zeroshot_configs, removal_threshold=0)

        # Consider making test scoring optional here
        score = config_scorer_test.score(zeroshot_configs)
        print(f'score: {score}')

        return zeroshot_configs, score
