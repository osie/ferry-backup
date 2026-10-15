package helm

import (
	"bytes"
	"errors"
	"io"
	"os"
	"os/exec"
	"path"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"testing"
	"time"

	"gopkg.in/yaml.v3"
)

type object map[string]any

func (o object) at(keys ...string) any {
	var cur any = map[string]any(o)
	for _, k := range keys {
		m, ok := cur.(map[string]any)
		if !ok {
			return nil
		}
		cur = m[k]
	}
	return cur
}

func (o object) str(keys ...string) string {
	s, _ := o.at(keys...).(string)
	return s
}

func (o object) list(keys ...string) []object {
	raw, _ := o.at(keys...).([]any)
	out := make([]object, 0, len(raw))
	for _, r := range raw {
		if m, ok := r.(map[string]any); ok {
			out = append(out, object(m))
		}
	}
	return out
}

func render(t *testing.T, args ...string) []object {
	t.Helper()
	helm, err := exec.LookPath("helm")
	if err != nil {
		if ci, _ := strconv.ParseBool(os.Getenv("CI")); ci {
			t.Fatal("helm is not on PATH and CI is set: install helm v3 before go test, or the chart is not checked at all")
		}
		t.Skip("SKIPPED, NOT PASSED: helm is not on PATH, so NOTHING in helm/ferry was rendered or checked. Install helm to run the chart tests; CI fails here instead.")
	}
	cmd := exec.Command(helm, append([]string{"template", "t", "ferry",
		"--set", "cloud.name=lab", "--set", "api.ingress.host=ferry.lab.example"}, args...)...)
	var out, stderr bytes.Buffer
	cmd.Stdout, cmd.Stderr = &out, &stderr
	if err := cmd.Run(); err != nil {
		t.Fatalf("helm template %v: %v\n%s", args, err, stderr.String())
	}
	dec := yaml.NewDecoder(&out)
	var docs []object
	for {
		var doc map[string]any
		if err := dec.Decode(&doc); errors.Is(err, io.EOF) {
			break
		} else if err != nil {
			t.Fatalf("decode the rendered chart: %v", err)
		}
		if doc != nil {
			docs = append(docs, object(doc))
		}
	}
	return docs
}

func find(t *testing.T, docs []object, kind, name string) object {
	t.Helper()
	for _, d := range docs {
		if d.str("kind") == kind && d.str("metadata", "name") == name {
			return d
		}
	}
	t.Fatalf("the chart rendered no %s %s", kind, name)
	return nil
}

func config(t *testing.T, docs []object, name, key string) object {
	t.Helper()
	cm := find(t, docs, "ConfigMap", name)
	var parsed map[string]any
	if err := yaml.Unmarshal([]byte(cm.str("data", key)), &parsed); err != nil {
		t.Fatalf("decode %s %s: %v", name, key, err)
	}
	return object(parsed)
}

func cephFiles(t *testing.T, workload object, container string) []string {
	t.Helper()
	spec := object(workload.at("spec", "template", "spec").(map[string]any))
	mount := ""
	for _, c := range spec.list("containers") {
		if c.str("name") != container {
			continue
		}
		for _, m := range c.list("volumeMounts") {
			if m.str("name") == "ceph" {
				mount = m.str("mountPath")
			}
		}
	}
	if mount == "" {
		t.Fatalf("container %s mounts no ceph volume", container)
	}
	var files []string
	for _, v := range spec.list("volumes") {
		if v.str("name") != "ceph" {
			continue
		}
		for _, src := range v.list("projected", "sources") {
			for _, kind := range []string{"secret", "configMap"} {
				for _, item := range src.list(kind, "items") {
					files = append(files, path.Join(mount, item.str("path")))
				}
			}
		}
	}
	return files
}

func requireFile(t *testing.T, files []string, who, why, file string) {
	t.Helper()
	if !slices.Contains(files, file) {
		t.Errorf("%s needs %s (%s), and its pod only has %v", who, file, why, files)
	}
}

func TestEveryCephIdentityAPodActsAsIsMountedInIt(t *testing.T) {
	cases := map[string][]string{
		"defaults":     nil,
		"renamed keys": {"--set", "ceph.users.manager=k8s-mgr,ceph.users.mover=k8s-mover,ceph.users.restore=ferry-k8s-restore"},
	}
	presets, _ := filepath.Glob("ferry/presets/*.yaml")
	for _, p := range presets {
		cases["preset "+strings.TrimSuffix(filepath.Base(p), ".yaml")] = []string{"-f", p}
	}
	for name, args := range cases {
		t.Run(name, func(t *testing.T) {
			docs := render(t, args...)
			mgr := config(t, docs, "t-ferry-manager", "manager.yaml")
			if mgr.str("ceph", "conf") == "" {
				t.Skip("this rendering does not read Ceph")
			}
			manager := cephFiles(t, find(t, docs, "Deployment", "t-ferry-manager"), "manager")
			requireFile(t, manager, "the manager", "ceph.conf", mgr.str("ceph", "conf"))
			requireFile(t, manager, "the manager", "its own identity",
				path.Join(path.Dir(mgr.str("ceph", "conf")), "ceph.client."+mgr.str("ceph", "user")+".keyring"))
			requireFile(t, manager, "the manager",
				"it seals standbys, promotes them and clones instant restores as the restore identity",
				mgr.str("ceph", "restore", "keyring"))

			mover := cephFiles(t, find(t, docs, "StatefulSet", "t-ferry-mover"), "mover")
			requireFile(t, mover, "a mover", "every backup work order names it", mgr.str("ceph", "mover", "keyring"))
			requireFile(t, mover, "a mover", "every restore and replay work order names it", mgr.str("ceph", "restore", "keyring"))
		})
	}
}

func TestTheKeyringJobIsRenamedWheneverItsPodTemplateChanges(t *testing.T) {
	jobName := func(args ...string) string {
		docs := render(t, append([]string{"--set", "ceph.keyrings.bootstrap.enabled=true"}, args...)...)
		for _, d := range docs {
			if d.str("kind") == "Job" && strings.Contains(d.str("metadata", "name"), "-ceph-keyrings-") {
				return d.str("metadata", "name")
			}
		}
		t.Fatalf("no keyring Job rendered for %v", args)
		return ""
	}
	base := jobName()
	for name, args := range map[string][]string{
		"another helper image": {"--set", "images.cephHelper=docker.io/openstackhelm/ceph-config-helper:other"},
		"another admin user":   {"--set", "ceph.keyrings.bootstrap.adminUser=client-admin"},
		"another admin secret": {"--set", "ceph.keyrings.bootstrap.adminSecret=other-key"},
		"another ceph.conf":    {"--set", "ceph.conf.configMap=other-etc"},
	} {
		if got := jobName(args...); got == base {
			t.Errorf("%s left the Job named %s; Kubernetes refuses to patch a Job's pod template", name, got)
		}
	}
	if again := jobName(); again != base {
		t.Errorf("the same values renamed the Job: %s then %s", base, again)
	}
}

func TestRemovingLeakedNovaDisksRendersOnlyWhenAsked(t *testing.T) {
	for name, tc := range map[string]struct {
		args []string
		want bool
	}{
		"defaults":                    {nil, false},
		"asked for":                   {[]string{"--set", "ceph.removeNovaOrphans=true"}, true},
		"values from before the knob": {[]string{"--set", "ceph.removeNovaOrphans=null"}, false},
	} {
		t.Run(name, func(t *testing.T) {
			mgr := config(t, render(t, tc.args...), "t-ferry-manager", "manager.yaml")
			got, ok := mgr.at("ceph", "remove_nova_orphans").(bool)
			if !ok || got != tc.want {
				t.Errorf("ceph.remove_nova_orphans rendered %v, want %v", mgr.at("ceph", "remove_nova_orphans"), tc.want)
			}
		})
	}
}

func TestValuesFromAReleaseBeforeTheDRSettingsStillRender(t *testing.T) {
	for name, args := range map[string][]string{
		"no dr.probe": {"--set", "dr.probe=null"},
		"no dr":       {"--set", "dr=null"},
	} {
		t.Run(name, func(t *testing.T) {
			mgr := config(t, render(t, args...), "t-ferry-manager", "manager.yaml")
			if got := mgr.str("dr", "run_deadline"); got != "12h" {
				t.Errorf("dr.run_deadline = %q, want the 12h default", got)
			}
			if enabled, _ := mgr.at("dr", "enabled").(bool); enabled {
				t.Error("dr.enabled rendered true from values that never set it")
			}
		})
	}
}

func TestTheMoverPodOutlastsItsDrain(t *testing.T) {
	for name, tc := range map[string]struct {
		args  []string
		drain time.Duration
	}{
		"defaults":                    {nil, 90 * time.Second},
		"a longer drain":              {[]string{"--set", "mover.drainTimeoutSeconds=300"}, 300 * time.Second},
		"values from before the knob": {[]string{"--set", "mover.drainTimeoutSeconds=null"}, 90 * time.Second},
	} {
		t.Run(name, func(t *testing.T) {
			docs := render(t, tc.args...)
			mv := config(t, docs, "t-ferry-mover", "mover.yaml")
			drain, err := time.ParseDuration(mv.str("drain_timeout"))
			if err != nil || drain != tc.drain {
				t.Fatalf("drain_timeout rendered %q (%v), want %s", mv.str("drain_timeout"), err, tc.drain)
			}
			grace, ok := find(t, docs, "StatefulSet", "t-ferry-mover").at("spec", "template", "spec", "terminationGracePeriodSeconds").(int)
			if !ok {
				t.Fatal("the mover pod sets no terminationGracePeriodSeconds, so Kubernetes kills it 30 seconds into a drain")
			}
			if got := time.Duration(grace) * time.Second; got != drain+time.Minute {
				t.Errorf("terminationGracePeriodSeconds = %s, want the drain plus a minute for the cut jobs to unwind and report (%s)", got, drain+time.Minute)
			}
		})
	}
}
