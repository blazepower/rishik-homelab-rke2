// Evaluate synthetic admission requests with the actual versioned Kyverno engine.
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"

	policies "github.com/kyverno/api/api/policies.kyverno.io/v1beta1"
	celengine "github.com/kyverno/kyverno/pkg/cel/engine"
	"github.com/kyverno/kyverno/pkg/cel/libs"
	"github.com/kyverno/kyverno/pkg/cel/matching"
	"github.com/kyverno/kyverno/pkg/cel/policies/vpol/compiler"
	vpolengine "github.com/kyverno/kyverno/pkg/cel/policies/vpol/engine"
	engineapi "github.com/kyverno/kyverno/pkg/engine/api"
	admissionv1 "k8s.io/api/admission/v1"
	admissionregistrationv1 "k8s.io/api/admissionregistration/v1"
	authenticationv1 "k8s.io/api/authentication/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
)

type testCase struct {
	ID                string                     `json:"id"`
	Policy            string                     `json:"policy"`
	Object            *unstructured.Unstructured `json:"object"`
	OldObject         *unstructured.Unstructured `json:"oldObject"`
	Operation         string                     `json:"operation"`
	Subresource       string                     `json:"subresource"`
	Username          string                     `json:"username"`
	Expected          string                     `json:"expected"`
	ExpectedException bool                       `json:"expectedException"`
	ExpectedActions   []string                   `json:"expectedActions"`
	LegacyExpected    string                     `json:"legacyExpected"`
}

type suite struct {
	Policies   []*policies.ValidatingPolicy `json:"policies"`
	Exceptions []*policies.PolicyException  `json:"exceptions"`
	Cases      []testCase                   `json:"cases"`
}

type result struct {
	ID           string   `json:"id"`
	Policy       string   `json:"policy"`
	Status       string   `json:"status"`
	RuleStatuses []string `json:"ruleStatuses"`
	Exceptions   int      `json:"exceptions"`
}

func run() error {
	if len(os.Args) != 3 {
		return fmt.Errorf("usage: native-engine suite.json result.json")
	}
	input, err := os.Open(os.Args[1])
	if err != nil {
		return err
	}
	defer input.Close()
	var tests suite
	decoder := json.NewDecoder(input)
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&tests); err != nil {
		return err
	}
	if len(tests.Policies) == 0 || len(tests.Cases) == 0 {
		return fmt.Errorf("empty policy or request suite")
	}
	var typed []policies.ValidatingPolicyLike
	for _, policy := range tests.Policies {
		typed = append(typed, policy)
	}
	provider, err := vpolengine.NewProvider(compiler.NewCompiler(), typed, tests.Exceptions)
	if err != nil {
		return err
	}
	engine := vpolengine.NewEngine(provider, func(name string) *corev1.Namespace {
		return &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{
			Name: name, Labels: map[string]string{"kubernetes.io/metadata.name": name},
		}}
	}, matching.NewMatcher())
	resources := map[string]string{
		"Pod": "pods", "Namespace": "namespaces", "Deployment": "deployments",
		"DaemonSet": "daemonsets", "StatefulSet": "statefulsets", "ReplicaSet": "replicasets",
		"ReplicationController": "replicationcontrollers", "Job": "jobs", "CronJob": "cronjobs",
	}
	var results []result
	ids := map[string]bool{}
	for _, test := range tests.Cases {
		if ids[test.ID] {
			return fmt.Errorf("duplicate request ID %s", test.ID)
		}
		ids[test.ID] = true
		resource := test.Object
		if resource == nil {
			resource = test.OldObject
		}
		if resource == nil {
			return fmt.Errorf("%s: no resource", test.ID)
		}
		gvk := resource.GroupVersionKind()
		plural, ok := resources[gvk.Kind]
		if !ok {
			return fmt.Errorf("%s: unknown fixture kind %s", test.ID, gvk.Kind)
		}
		var object, oldObject runtime.Object
		if test.Object != nil {
			object = test.Object
		}
		if test.OldObject != nil {
			oldObject = test.OldObject
		}
		request := celengine.Request(
			libs.GetLibsCtx(), gvk,
			schema.GroupVersionResource{Group: gvk.Group, Version: gvk.Version, Resource: plural},
			test.Subresource, resource.GetName(), resource.GetNamespace(),
			admissionv1.Operation(test.Operation),
			authenticationv1.UserInfo{Username: test.Username}, object, oldObject, false, nil,
		)
		// Exercise the real wire representation, including null oldObject on CREATE.
		wire, err := json.Marshal(request.Request)
		if err != nil {
			return err
		}
		var admission admissionv1.AdmissionRequest
		if err := json.Unmarshal(wire, &admission); err != nil {
			return err
		}
		response, err := engine.Handle(
			context.Background(), celengine.RequestFromAdmission(libs.GetLibsCtx(), admission),
			func(policy policies.ValidatingPolicyLike) bool { return policy.GetName() == test.Policy },
		)
		if err != nil {
			return fmt.Errorf("%s: %w", test.ID, err)
		}
		if len(response.Policies) != 1 {
			return fmt.Errorf("%s: expected exactly one policy response, received %d",
				test.ID, len(response.Policies))
		}
		if len(response.Policies[0].Actions) != len(test.ExpectedActions) {
			return fmt.Errorf("%s: changed original validation actions", test.ID)
		}
		for _, action := range test.ExpectedActions {
			if !response.Policies[0].Actions.Has(admissionregistrationv1.ValidationAction(action)) {
				return fmt.Errorf("%s: missing original validation action %s", test.ID, action)
			}
		}
		actual := result{ID: test.ID, Policy: test.Policy, Status: "skip", RuleStatuses: []string{}}
		for _, rule := range response.Policies[0].Rules {
			var status string
			switch rule.Status() {
			case engineapi.RuleStatusPass:
				status = "pass"
			case engineapi.RuleStatusFail:
				status = "fail"
			case engineapi.RuleStatusSkip:
				status = "skip"
			default:
				return fmt.Errorf("%s: native evaluation error: %v", test.ID, rule)
			}
			actual.RuleStatuses = append(actual.RuleStatuses, status)
			actual.Exceptions += len(rule.Exceptions())
			if len(actual.RuleStatuses) == 1 {
				actual.Status = status
			} else if actual.Status != status {
				return fmt.Errorf("%s: unexpected mixed rule statuses %v", test.ID, actual.RuleStatuses)
			}
		}
		results = append(results, actual)
		if actual.Status != test.Expected || (actual.Exceptions > 0) != test.ExpectedException {
			data, _ := json.Marshal(actual)
			return fmt.Errorf("%s: expected %s exception=%t, received %s",
				test.ID, test.Expected, test.ExpectedException, data)
		}
	}
	output, err := os.Create(os.Args[2])
	if err != nil {
		return err
	}
	defer output.Close()
	encoder := json.NewEncoder(output)
	encoder.SetIndent("", "  ")
	return encoder.Encode(results)
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
